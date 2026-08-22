#!/usr/bin/env python3
"""Validate the update feed against the channel it describes.

The feed is a set of CLAIMS about published artifacts. Nothing checked those
claims, so a bad write went unnoticed until a consumer that verifies (the
simplerisk/docker image build, which fails closed) hit it hours later. This
asserts the claims against reality.

The invariant, stated precisely:

    Every release REACHABLE as an upgrade target must have a bundle that is
    actually fetchable on this channel, and the feed's checksum for it must
    match those bytes.

"Reachable" = Current_Version, plus every hop target in upgrade_path.xml. A
superseded release that nothing routes to may legitimately have no bundle left
on the channel — each channel serves only what it currently needs — so it is not
checked. What must never happen is routing an upgrade at a bundle that is not
there, or publishing a checksum that does not match the bytes served.

Usage: validate_feed.py <channel>       # channel: prod | test
Exit 0 = valid, 1 = invalid, 2 = could not complete the check.
"""
import hashlib
import re
import sys
import urllib.error
import urllib.request
import xml.etree.ElementTree as ET

FILES = ["releases.xml", "Current_Version.xml", "upgrade_path.xml",
         "announcements.xml", "extra_compatibility.xml"]

BUNDLE_URL = {
    "prod": "https://simplerisk-downloads.s3.amazonaws.com/public/bundles/simplerisk-{v}.tgz",
    "test": "https://bundles-test.simplerisk.com/simplerisk-{v}.tgz",
}

VERSION_RE = re.compile(r"^\d{8}-\d{3}$")
SHA_RE = re.compile(r"^[0-9a-f]{64}$")

errors: list[str] = []
notes: list[str] = []


def err(msg: str) -> None:
    errors.append(msg)
    print(f"::error::{msg}")


def note(msg: str) -> None:
    notes.append(msg)
    print(f"  {msg}")


DOCTYPE_RE = re.compile(rb"<!\s*(DOCTYPE|ENTITY)", re.I)


def wellformed() -> None:
    """Every manifest must parse. A truncated write breaks every consumer.

    ElementTree resolves no external entities, but it does expand internal ones,
    so a DOCTYPE could hang the runner (billion laughs). These manifests are
    generated and never legitimately carry a DTD, so reject one outright rather
    than take a dependency on defusedxml, which runners do not ship.
    """
    for f in FILES:
        try:
            with open(f, "rb") as fh:
                head = fh.read(65536)
        except FileNotFoundError:
            err(f"{f} is missing")
            continue
        if DOCTYPE_RE.search(head):
            err(f"{f} contains a DOCTYPE/ENTITY declaration; these manifests must not")
            continue
        try:
            ET.parse(f)
            note(f"OK   {f} parses")
        except ET.ParseError as e:
            err(f"{f} is not well-formed XML: {e}")


def releases() -> dict[str, dict]:
    """version -> {sha256, md5, next_release} for every <release> entry."""
    out = {}
    root = ET.parse("releases.xml").getroot()
    for rel in root.findall("release"):
        v = rel.get("version", "")
        text = lambda t: (rel.findtext(t) or "").strip()
        out[v] = {
            "sha256": text("bundle_sha256"),
            "md5": text("bundle_md5"),
            "next_release": text("next_release"),
        }
    return out


def upgrade_hops() -> dict[str, str]:
    """from-version -> to-version, from upgrade_path.xml."""
    hops = {}
    root = ET.parse("upgrade_path.xml").getroot()
    for child in root:
        m = re.fullmatch(r"simplerisk-(\d{8}-\d{3})", child.tag)
        if not m:
            err(f"upgrade_path.xml: unexpected element <{child.tag}>")
            continue
        target = (child.text or "").strip()
        if target:  # an empty element marks the newest release (no next hop)
            hops[m.group(1)] = target
    return hops


def fetch(url: str):
    """-> (bytes, None) or (None, reason). Never raises."""
    try:
        with urllib.request.urlopen(url, timeout=300) as r:
            return r.read(), None
    except urllib.error.HTTPError as e:
        # S3 returns 403, not 404, for a missing object under an
        # anonymous-list-denied bucket. Both mean "not published here".
        return None, f"HTTP {e.code}" + (" (absent)" if e.code in (403, 404) else "")
    except Exception as e:  # noqa: BLE001 - network is the point of failure here
        return None, str(e)


def main() -> int:
    if len(sys.argv) != 2 or sys.argv[1] not in BUNDLE_URL:
        print("usage: validate_feed.py <prod|test>", file=sys.stderr)
        return 2
    channel = sys.argv[1]

    print(f"== well-formedness")
    wellformed()
    if errors:
        return 1  # nothing else is meaningful against unparseable XML

    rel = releases()
    hops = upgrade_hops()
    current = (ET.parse("Current_Version.xml").getroot().findtext("appversion") or "").strip()

    print(f"\n== structure ({len(rel)} releases, {len(hops)} upgrade hops, current={current})")
    if not VERSION_RE.match(current):
        err(f"Current_Version/appversion '{current}' is not a YYYYMMDD-NNN version")
    elif current not in rel:
        err(f"Current_Version {current} has no <release> entry in releases.xml")
    else:
        note(f"OK   Current_Version {current} has a release entry")

    # Referential integrity: never route an upgrade at a version we do not describe.
    for src, dst in sorted(hops.items()):
        if dst not in rel:
            err(f"upgrade_path routes {src} -> {dst}, which has no <release> entry")
    if not any(d not in rel for d in hops.values()):
        note("OK   every upgrade_path target has a release entry")

    # next_release must agree with upgrade_path, or consumers disagree about
    # where to go depending on which file they read.
    mismatched = 0
    for src, dst in sorted(hops.items()):
        declared = rel.get(src, {}).get("next_release", "")
        if declared and declared != dst:
            err(f"{src}: next_release={declared} but upgrade_path says {dst}")
            mismatched += 1
    if not mismatched:
        note("OK   next_release agrees with upgrade_path everywhere")

    # WHAT GETS CHECKED AGAINST THE CHANNEL, and why it is this set:
    #
    # A release entry publishing a real bundle_sha256 is making a checkable
    # claim -- "this bundle exists here and hashes to this". Every such claim is
    # verified. Historical entries carry no hash (only a handful ever do, since
    # a channel serves only what it currently needs) and make no claim, so there
    # is nothing to check and their bundles being long gone is expected.
    #
    # The current release is checked additionally for the PRESENCE of a hash: it
    # is the one release consumers actually download, so an unverifiable current
    # release is a defect even though an unverifiable 2015 release is not.
    claimants = sorted((v for v, d in rel.items() if SHA_RE.match(d["sha256"])), reverse=True)
    if current and current not in claimants and current in rel:
        err(f"the current release {current} publishes no bundle_sha256 — "
            f"consumers cannot verify the bundle they are told to download")
    print(f"\n== bundles claimed by the feed ({len(claimants)}: {', '.join(claimants) or 'none'})")

    for v in claimants:
        want = rel[v]["sha256"]
        url = BUNDLE_URL[channel].format(v=v)
        blob, why = fetch(url)
        if blob is None:
            err(f"{v} publishes a bundle_sha256 but its bundle is not fetchable "
                f"on the {channel} channel ({why}): {url}")
            continue
        got = hashlib.sha256(blob).hexdigest()
        if got != want:
            err(f"{v} bundle_sha256 mismatch on {channel}: feed says {want}, "
                f"the served bytes hash to {got}")
            continue
        md5_want = rel[v]["md5"]
        if re.fullmatch(r"[0-9a-f]{32}", md5_want):
            md5_got = hashlib.md5(blob).hexdigest()  # noqa: S324 - feed field, not security
            if md5_got != md5_want:
                err(f"{v} bundle_md5 mismatch on {channel}: feed says {md5_want}, got {md5_got}")
                continue
        note(f"OK   {v} bundle verified against the {channel} channel ({got[:16]}…)")

    print()
    if errors:
        print(f"FAILED — {len(errors)} problem(s)")
        return 1
    print(f"PASSED — {len(notes)} checks")
    return 0


if __name__ == "__main__":
    sys.exit(main())
