# local-rag-mcp — hướng dẫn ngắn

`local-rag-mcp` là bộ lập chỉ mục tài liệu local-first và MCP server chạy qua stdio cho các agent
đáng tin cậy. Tài liệu gốc trong thư mục local hoặc Google Drive vẫn là nguồn chuẩn; dữ liệu trích
xuất, FTS5, metadata, citation, OCR review và vector cache (tuỳ chọn) nằm trong `~/.local-rag`.

README tiếng Anh là tài liệu chuẩn: [README.md](README.md).

## Trạng thái phát hành

**v0.7.1** đã phát hành ngày 2026-09-01. Tải wheel và source archive duy nhất từ
[GitHub Release](https://github.com/phamviet86/local-rag-mcp/releases/tag/v0.7.1); hiện dự án chưa
phát hành package lên PyPI.

| Mục đích | Tên |
| --- | --- |
| Product, repository, CLI, MCP server | `local-rag-mcp` |
| Python distribution | `phamviet-local-rag-mcp` |

PyPI đã có một dự án khác tên `local-rag-mcp`. Vì vậy tuyệt đối không dùng `pip install
local-rag-mcp` hoặc `pip install 'local-rag-mcp[...]'`. Cài distribution duy nhất
`phamviet-local-rag-mcp` từ GitHub Release của repository này.

Dự án dùng license [Apache-2.0](LICENSE), độc lập và không liên kết với dự án PyPI trùng tên.

## Cài trên máy khác

Cài trực tiếp wheel v0.7.1 vào virtual environment riêng (Python 3.11–3.13); không cần clone mã
nguồn. [Trang release](https://github.com/phamviet86/local-rag-mcp/releases/tag/v0.7.1) công bố
SHA-256 cho từng artifact:

```bash
mkdir -p "$HOME/.local/share/local-rag-mcp"
python3.11 -m venv "$HOME/.local/share/local-rag-mcp/.venv"
. "$HOME/.local/share/local-rag-mcp/.venv/bin/activate"
python -m pip install \
  "https://github.com/phamviet86/local-rag-mcp/releases/download/v0.7.1/phamviet_local_rag_mcp-0.7.1-py3-none-any.whl"
local-rag-mcp setup --no-ocr
local-rag-mcp doctor --json
```

Nếu cần xác minh chặt chẽ hơn, hãy tải wheel và `SHA256SUMS` rồi kiểm checksum trước khi cài. Quy
trình copy-paste riêng cho macOS và Linux nằm trong
[docs/deployment.md](docs/deployment.md#verify-and-install-a-release-wheel); tải thành công không
đồng nghĩa đã xác minh checksum.

Extras cài từ cùng wheel release bằng distribution đúng:

```bash
python -m pip install \
  "phamviet-local-rag-mcp[local-embeddings] @ https://github.com/phamviet86/local-rag-mcp/releases/download/v0.7.1/phamviet_local_rag_mcp-0.7.1-py3-none-any.whl"
python -m pip install \
  "phamviet-local-rag-mcp[google-drive] @ https://github.com/phamviet86/local-rag-mcp/releases/download/v0.7.1/phamviet_local_rag_mcp-0.7.1-py3-none-any.whl"
```

`setup --full` tải và kiểm tra runtime OCR local; `setup --no-ocr` vẫn hỗ trợ text/Office/native PDF
text và full-text search. Cài xong nhưng chưa có source là trạng thái rỗng hợp lệ nhưng chưa sẵn sàng
truy xuất: `status` và `doctor` trả mã `2`, còn truy xuất trả `no_enabled_sources`, cho đến khi
operator chủ động thêm nguồn.

```bash
local-rag-mcp source add-local notes /absolute/path/to/documents
local-rag-mcp reconcile --source notes
```

Không tự giả định thư mục nguồn, Google Drive, remote embedding hoặc background service. Xem
[docs/setup.md](docs/setup.md) và [docs/deployment.md](docs/deployment.md) để triển khai, backup,
nâng cấp, rollback và gỡ cài đặt. Tham chiếu tính năng/lệnh đầy đủ nằm trong
[docs/reference.md](docs/reference.md).

## Kết nối Codex MCP

Chạy profile `reader` mặc định với đường dẫn tuyệt đối:

```bash
codex mcp add local-rag-mcp \
  --env LOCAL_RAG_MCP_HOME="$HOME/.local-rag" \
  --env LOCAL_RAG_MCP_PROFILE=reader \
  -- "$HOME/.local/share/local-rag-mcp/.venv/bin/local-rag-mcp-server"
codex mcp get local-rag-mcp
```

Kết nối lại Codex rồi kiểm tra `doctor`, `sources`, `search`. Chỉ cấp profile `reviewer` hoặc `admin`
cho tiến trình local đáng tin cậy. Đọc [docs/agents.md](docs/agents.md) và
[SECURITY.md](SECURITY.md) trước khi cho agent truy cập dữ liệu.
