# DOGFOOD — boaz

## 2026-11-12 — park VideoPsalm layout export in ignored scratch (branch `chore/videopsalm-scratch`)

Goal: keep an untracked VideoPsalm app-layout export out of the repo — it is
session scratch (zero references anywhere in code/tests/docs), not boaz config
and not a test fixture.

### Scores (strict gate — overall = minimum)

| Surface | Score | Evidence |
|---|---|---|
| test suite | 10/10 | `uv run pytest tests/ -q` → 27 passed (two full runs). Jury observed one flake in `test_sweep_spares_other_pool` (launch `--wait 15` nonzero) that passed on immediate rerun and in isolation — untouched by this diff (waiver W2) |
| doctor | 10/10 | `boaz doctor` → ✅ X apps capturable: yes, ✅ GL apps capturable: yes, exit 0 |
| repo hygiene | 10/10 | move verified: `scratch/out/videopsalm-layout.json` present, root clean, `scratch/` in .gitignore; zero references proven by grep across code/tests/docs |

### Bug class

n/a for this diff (bookkeeping change, no behavior).

### Gaps (honestly unmet)

- `tesseract` missing in this environment (doctor ❌ with install hint): `find`/`assert_text` use the fallback path; test suite green regardless — pre-existing, unrelated to this diff.

### Branch

`chore/videopsalm-scratch` — PR filed from this branch.
