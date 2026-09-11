# agent-ops-warehouse

**このファイルは地図に徹する。詳細を書き込まない。**(`AI_DRIVEN_DEV_PRACTICAL_FLOW_20260905.md` 1-1節準拠)

## 技術スタックの要約
BigQuery(Terraform管理)+ Python(loader)+ FastAPI(Cloud Run上のRAG API・P3)+ dbt(raw→staging→marts変換)。GCPの無料枠内で運用。

## ディレクトリ構成
- `loader/` — git履歴・Markdown・公開ログ・KPIをBigQueryへ取り込むPythonローダー
- `dbt/` — raw→staging→martsのSQL変換(dbt管理)
- `api/` — FastAPI製RAG API(Cloud Run・P3・`GET /health` / `POST /query`)
- `terraform/` — GCPインフラ全体(全リソースの正本)
- `scripts/` — 運用スクリプト
- `tests/` — pytest
- `reference/` — 参照資料

## 主要コマンド
```bash
source .venv/bin/activate
pytest                            # テスト実行
python -m loader                  # ローダー実行(週次・手動起動)
terraform -chdir=terraform plan   # インフラ差分確認
```

## 開発ルールへの誘導
このリポジトリは公開リポジトリのため、詳細な仕様・チェックリストは親環境側に置く(`GOVERNANCE-POINTER.md`参照・gitignore対象で公開履歴には出ない)。学びはStudio全体の`_ops/lessons/`へ(R5)。
