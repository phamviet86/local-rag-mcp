# Search evaluation baseline (full-text only)

- Date: 2026-11-07
- Base commit: a69e3a4 plus the phase 5 changes (MRR/nDCG, window-chunk context prefix, migration 12); phase 5 commit: 6211935, where the baseline numbers were produced
- Mode: `--mode full_text`, `embedding_provider=none`, reranker off, `--no-ocr`. No embedding, rerank or network call was made.
- Question set: `testdata/eval/legal-questions.jsonl` (24 questions, gold = legal cite_keys; a gold Dieu also accepts its khoan chunks).
- Metrics: recall@k = share of questions with a gold hit in the top k; MRR@10 = mean of 1/rank of the first gold hit (0 beyond rank 10); nDCG@10 = binary DCG over all gold keys (each credited once, khoan prefix rule) divided by the ideal DCG over min(len(gold), 10) positions.

## Commands

Run in a throwaway home (never `~/.local-rag`):

```bash
H=$(mktemp -d)
local-rag-mcp --home "$H" setup --no-ocr
local-rag-mcp --home "$H" source add-local legal testdata/legal
local-rag-mcp --home "$H" reconcile
local-rag-mcp --home "$H" eval testdata/eval/legal-questions.jsonl --mode full_text --k 10   # run A
local-rag-mcp --home "$H" source add-local scanned testdata/scanned
local-rag-mcp --home "$H" reconcile
local-rag-mcp --home "$H" eval testdata/eval/legal-questions.jsonl --mode full_text --k 10   # run B
```

The question set also targets documents in `testdata/scanned` (58/2010/QH12, 01/2012/NĐ-CP), so run A
is capped by `gold_not_indexed`. Run B is the meaningful baseline. Under `--no-ocr` some pages are
routed to the review queue (`complex_layout`); no OCR was run, so scores reflect native-text
extraction only. Run A misses the default `--target 0.8` (recall@10); run B meets it.

## Run A: `testdata/legal` only

| group | n | recall@1 | recall@3 | recall@10 | MRR@10 | nDCG@10 |
|---|---|---|---|---|---|---|
| overall | 24 | 0.4167 | 0.5 | 0.5833 | 0.4625 | 0.4908 |
| type=docno | 4 | 0.0 | 0.25 | 0.25 | 0.0833 | 0.125 |
| type=question | 3 | 0.3333 | 0.3333 | 0.6667 | 0.375 | 0.4385 |
| type=topic | 17 | 0.5294 | 0.5882 | 0.6471 | 0.5672 | 0.5861 |
| diacritics=no | 4 | 0.5 | 0.5 | 0.75 | 0.5357 | 0.5833 |
| diacritics=yes | 20 | 0.4 | 0.5 | 0.55 | 0.4479 | 0.4723 |

Failure groups: {'gold_not_indexed': 7, 'wrong_chunk_right_document': 3}

## Run B: `testdata/legal` + `testdata/scanned` (baseline)

| group | n | recall@1 | recall@3 | recall@10 | MRR@10 | nDCG@10 |
|---|---|---|---|---|---|---|
| overall | 24 | 0.6667 | 0.7917 | 0.9167 | 0.7378 | 0.7795 |
| type=docno | 4 | 0.5 | 0.75 | 1.0 | 0.6146 | 0.7039 |
| type=question | 3 | 0.3333 | 0.6667 | 1.0 | 0.5417 | 0.6488 |
| type=topic | 17 | 0.7647 | 0.8235 | 0.8824 | 0.8015 | 0.8204 |
| diacritics=no | 4 | 0.75 | 0.75 | 1.0 | 0.7812 | 0.8289 |
| diacritics=yes | 20 | 0.65 | 0.8 | 0.9 | 0.7292 | 0.7696 |

Failure groups: {'wrong_chunk_right_document': 2}
