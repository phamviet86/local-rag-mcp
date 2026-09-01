# local-rag-mcp — hướng dẫn ngắn

`local-rag-mcp` là bộ lập chỉ mục tài liệu local-first và MCP server chạy qua stdio cho các agent
đáng tin cậy. Tài liệu gốc trong thư mục local hoặc Google Drive vẫn là nguồn chuẩn; dữ liệu trích
xuất, FTS5, metadata, citation, OCR review và vector cache (tuỳ chọn) nằm trong `~/.local-rag`.

README tiếng Anh là tài liệu chuẩn: [README.md](README.md).

## Trạng thái phát hành

Mục tiêu hiện tại là release candidate **v0.7.0**. Repository đang private và **chưa có GitHub
Release hoặc PyPI release**. Không coi đây là bản đã phát hành, không cài từ artifact không rõ nguồn.

| Mục đích | Tên |
| --- | --- |
| Product, repository, CLI, MCP server | `local-rag-mcp` |
| Python distribution | `phamviet-local-rag-mcp` |

PyPI đã có một dự án khác tên `local-rag-mcp`. Vì vậy tuyệt đối không dùng `pip install
local-rag-mcp` hoặc `pip install 'local-rag-mcp[...]'`. Khi maintainer công bố bản chính thức, cài
wheel từ GitHub Release trước; PyPI (nếu có) phải dùng tên duy nhất `phamviet-local-rag-mcp`.

Dự án dùng license [Apache-2.0](LICENSE), độc lập và không liên kết với dự án PyPI trùng tên.

## Cài trên máy khác

Sau khi có GitHub Release, tải đúng wheel đính kèm release, kiểm tra checksum do release notes cung
cấp, sau đó cài trong virtual environment riêng (Python 3.11–3.13):

```bash
mkdir -p "$HOME/.local/share/local-rag-mcp"
python3.11 -m venv "$HOME/.local/share/local-rag-mcp/.venv"
. "$HOME/.local/share/local-rag-mcp/.venv/bin/activate"
python -m pip install "$HOME/Downloads/phamviet_local_rag_mcp-0.7.0-py3-none-any.whl"
local-rag-mcp setup --no-ocr
local-rag-mcp doctor --json
```

Extras có thể cài trực tiếp từ wheel release (thay đúng filename đã phát hành):

```bash
python -m pip install "phamviet-local-rag-mcp[local-embeddings] @ file://$HOME/Downloads/phamviet_local_rag_mcp-0.7.0-py3-none-any.whl"
python -m pip install "phamviet-local-rag-mcp[google-drive] @ file://$HOME/Downloads/phamviet_local_rag_mcp-0.7.0-py3-none-any.whl"
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
