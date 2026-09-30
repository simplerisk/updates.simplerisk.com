# updates.simplerisk.com

Two long-lived branches, one per update channel:

| Branch | Channel | Written by |
|---|---|---|
| `updates.simplerisk.com` | GA (prod) | `update_feeds.sh` in `sync_code_repo.yml`, with the real GA asset hashes |
| `updates-test.simplerisk.com` | testing | `update_feeds.sh` in `publish-bundle.yml`, plus the RC retraction step |

**The branches are not meant to converge. Do not merge one into the other.**
They differ on purpose: the test feed keeps hops for withdrawn RCs and blanks
checksums for bundles `bundles-test` no longer serves, while prod records real GA
hashes. Merging `updates-test` into prod overwrites GA truth with RC truth, and
`simplerisk/docker` fails closed on the resulting checksum mismatch. The
`Block test-branch promotion` check refuses that PR.

## Making a change to the prod feed by hand

For one-off changes such as VM appliance digests:

1. Branch off `updates.simplerisk.com`.
2. Change only the values you mean to (for VM digests, the four
   `virtualbox_*` / `vmware_*` values of that release).
3. `python3 scripts/validate_feed.py prod`
4. Open a PR into `updates.simplerisk.com`. `Validate feed against its channel`
   must pass.

If the same values belong on the test feed, make a separate change there. See #139
and #145 for worked examples.
