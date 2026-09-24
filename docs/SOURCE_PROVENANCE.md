# V1 Source Provenance Review

## Boundary

This is an engineering release-gate record, not legal advice or legal clearance.
`config/v1_source_provenance.json` covers all 22 distinct treebanks referenced by
the 26 public V1 catalog descriptors. It records the official Universal
Dependencies release, repository commit, source file, LICENSE and README
fingerprints, required attribution summary, and unresolved rights decision for
each treebank.

The manifest deliberately does not update challenge descriptors or evaluation
contracts. Those immutable draft snapshots remain the artifacts exercised by the
GPU evidence. Provenance promotion and activation require a reviewed release
workflow and, where necessary, new contract snapshots and matching GPU evidence.

## Verification

The catalog source files were compared with official Universal Dependencies 2.18,
released 2026-05-15 under tag `r2.18`. All 22 files match their pinned upstream
files after the repository's Windows CRLF checkout is normalized to the LF bytes
stored by Git and upstream. The manifest's internal report SHA-256 is
`7fd8e9b56661c6ab2fc15e2667291d4097f4f5d2fed932817b55afaa7ac5dd76`.

`tests/test_source_provenance.py` verifies offline that:

- the manifest's internal hash is valid;
- every public catalog descriptor maps to exactly one provenance entry;
- every pinned local source path exists and matches its canonical LF SHA-256;
- release commits and LICENSE/README fingerprints are structurally complete; and
- every challenge remains `draft` while each rights decision is `review_required`
  or `hold`.

## Review Status

| Treebank | Annotation license recorded upstream | Decision | Principal unresolved issue |
| --- | --- | --- | --- |
| Arabic PUD | CC BY-SA 3.0 | Review required | PUD underlying sentences may be copyrighted |
| Chinese Beginner | CC BY-NC-SA 3.0 | Hold | Noncommercial terms expressly restrict advertising-revenue applications |
| Chinese GSD | CC BY-SA 4.0 | Review required | Relicensing applies to annotations, not underlying content |
| Chinese GSDSimp | CC BY-SA 4.0 | Review required | Underlying-content rights remain separate |
| Danish DDT | CC BY-SA 4.0 | Review required | Confirm project use and attribution plan |
| German HDT | CC BY-SA 4.0 annotations | Hold | Heise text distribution is limited to academic use |
| English CHILDES | CC BY-SA 4.0 | Review required | Per-corpus citation plus child-data privacy and ethics review |
| English EWT | CC BY-SA 4.0 annotations/database | Review required | LDC2012T13 sentence rights vary by source |
| English GUM | CC BY-NC-SA 4.0 | Hold | Noncommercial and source-specific terms across mixed genres |
| Spanish AnCora | CC BY 4.0 | Review required | Confirm attribution and intended redistribution |
| French FQB | LGPL-LR | Review required | License-specific modification and source-supply obligations |
| Hebrew HTB | CC BY-NC-SA 4.0 | Review required | Noncommercial use and Haaretz text rights |
| Hindi HDTB | CC BY-NC-SA 4.0 | Review required | Noncommercial use and unspecified news publishers |
| Hungarian Szeged | CC BY-NC-SA 3.0 | Review required | Noncommercial use and newspaper publisher text |
| Italian KIParlaForest | CC BY-NC-SA 4.0 | Hold | KIParla requires agreement before inclusion in another resource |
| Japanese PUD | CC BY-SA 3.0 | Review required | PUD underlying sentences may be copyrighted |
| Korean Kaist | CC BY-SA 4.0 | Review required | Original genre-specific text rights are not separately established |
| Dutch LassySmall | Inconsistent BY-SA/BY-NC-SA document | Review required | Upstream license clarification is required |
| Portuguese CINTIL | CC BY-NC-ND 4.0 | Hold | No-derivatives terms conflict with converted or derived exercises |
| Russian SynTagRus | CC BY-NC-SA 4.0 | Review required | Noncommercial, heterogeneous third-party texts |
| Swedish Talbanken | CC BY-SA 4.0 | Review required | Confirm attribution and intended redistribution |
| Thai PUD | CC BY-SA 3.0 | Review required | PUD underlying sentences may be copyrighted |

## Activation Consequences

- Do not enable any current draft solely because the upstream file and annotation
  license are now identified.
- Treat text delivered to a browser or model API as a redistribution use for the
  purpose of the release review.
- Keep attribution and license obligations attached to each treebank; do not apply
  one project-wide data license to every source.
- Obtain written clearance or replace `Chinese Beginner`, `German HDT`, `English
  GUM`, `Italian KIParlaForest`, and `Portuguese CINTIL` before activation where
  the intended deployment conflicts with their recorded restrictions.
- Define a reviewed promotion manifest that binds source provenance, rights
  approval, clean Git release identity, deployed contract hash, and GPU evidence.
  The current `external_activation_ready` property is necessary but not a complete
  production authorization control.
