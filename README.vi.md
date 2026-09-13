# local-rag-mcp — hướng dẫn ngắn

`local-rag-mcp` là bộ lập chỉ mục tài liệu local-first và MCP server chạy qua stdio cho các agent
đáng tin cậy. Tài liệu gốc trong thư mục local hoặc Google Drive vẫn là nguồn chuẩn; dữ liệu trích
xuất, FTS5, metadata, citation, OCR review và vector cache (tuỳ chọn) nằm trong `~/.local-rag`.

README tiếng Anh là tài liệu chuẩn: [README.md](README.md).

## Trạng thái phát hành

**v0.9.0** bổ sung skill cài đặt và sử dụng cho agent. Tải wheel và source archive duy nhất từ
[GitHub Release](https://github.com/phamviet86/local-rag-mcp/releases/tag/v0.9.0); hiện dự án chưa
phát hành package lên PyPI.

| Mục đích | Tên |
| --- | --- |
| Product, repository, CLI, MCP server | `local-rag-mcp` |
| Python distribution | `phamviet-local-rag-mcp` |

PyPI đã có một dự án khác tên `local-rag-mcp`. Vì vậy tuyệt đối không dùng `pip install
local-rag-mcp` hoặc `pip install 'local-rag-mcp[...]'`. Cài distribution duy nhất
`phamviet-local-rag-mcp` từ GitHub Release của repository này.

Dự án dùng license [Apache-2.0](LICENSE), độc lập và không liên kết với dự án PyPI trùng tên.

## Cài bằng agent

Đưa README này cho agent trên máy trạm và yêu cầu cài đặt. Agent kiểm tra trạng thái có sẵn, giữ
các lựa chọn đã thống nhất, cài package và hai skill, sau đó đọc `local-rag-setup/SKILL.md` đã cài để
tự thực hiện cấu hình và kiểm chứng. Bạn chỉ cung cấp thông tin còn thiếu thực sự cần thiết như
thư mục local, folder ID, tài khoản hoặc đường dẫn file OAuth; đăng nhập và nhập secret thực hiện
trực tiếp trên máy, không dán vào chat. Full-text search không cần API key.

Cài wheel v0.9.0 trên macOS/Linux với Python 3.11–3.13; không cần clone repository. Agent kiểm tra
và tái sử dụng môi trường cài đặt phù hợp nếu đã có. Release kèm `SHA256SUMS`; xem
[quy trình xác minh](docs/deployment.md#verify-and-install-a-release-wheel) khi cần kiểm tra trước khi cài:

```bash
python3.11 -m venv "$HOME/.local/share/local-rag-mcp/.venv"
"$HOME/.local/share/local-rag-mcp/.venv/bin/python" -m pip install \
  "https://github.com/phamviet86/local-rag-mcp/releases/download/v0.9.0/phamviet_local_rag_mcp-0.9.0-py3-none-any.whl"
"$HOME/.local/share/local-rag-mcp/.venv/bin/local-rag-mcp" install-skills
```

Lệnh cuối trả đường dẫn tuyệt đối của **local-rag-setup** (cấu hình, kết nối, kiểm chứng) và
**local-rag** (tìm, đọc, trích nguồn). Agent đọc các file ngay để tiếp tục; tải lại ứng dụng nếu cần
để lần sau tự phát hiện skill. Thư mục mặc định là `$CODEX_HOME/skills` nếu được đặt, nếu không là
`~/.agents/skills`; chọn nơi khác bằng `--dest /duong/dan/tuyet/doi`.

`--dry-run` xem trước, `--check` kiểm tra không ghi dữ liệu; `--replace` cập nhật skill do bộ cài này
quản lý khi nội dung khác. Cài lại nội dung giống nhau không thay đổi file. Bộ cài giữ skill khác và
cấu hình client; từ chối thư mục trùng tên chưa được quản lý hoặc symlink, kể cả với `--replace`.
Mỗi skill có tham chiếu runtime không chứa secret, giúp tìm executable bằng đường dẫn tuyệt đối khi
đang ở ngoài repo hoặc chưa thêm virtualenv vào PATH. Cài extras từ cùng checkout/wheel đã chọn.
Sau nâng cấp hoặc chuyển môi trường, chạy lại `install-skills --replace`.

Ví dụ yêu cầu agent: “Cài repository này và hai skill trên máy, rồi thiết lập tìm tài liệu trong thư
mục [path] / Google Drive [folder ID]. Giữ cấu hình đã có và chỉ hỏi thông tin bắt buộc còn thiếu.”
Agent phải kiểm chứng `search` và `read` qua MCP thực tế, hoặc báo rõ còn cần kết nối lại client.
Việc sao chép skill chưa tự đăng ký MCP hay bật dịch vụ nền; skill setup hướng dẫn agent hoàn thành
các thao tác được giao. Xem [docs/setup.md](docs/setup.md).

## Cài thủ công và tích hợp tuỳ chọn

Cài trực tiếp wheel v0.9.0 vào virtual environment riêng (Python 3.11–3.13); không cần clone mã
nguồn. [Trang release](https://github.com/phamviet86/local-rag-mcp/releases/tag/v0.9.0) công bố
SHA-256 cho từng artifact:

```bash
mkdir -p "$HOME/.local/share/local-rag-mcp"
python3.11 -m venv "$HOME/.local/share/local-rag-mcp/.venv"
. "$HOME/.local/share/local-rag-mcp/.venv/bin/activate"
python -m pip install \
  "https://github.com/phamviet86/local-rag-mcp/releases/download/v0.9.0/phamviet_local_rag_mcp-0.9.0-py3-none-any.whl"
local-rag-mcp setup --no-ocr
local-rag-mcp install-skills
local-rag-mcp doctor --json
```

Nếu cần xác minh chặt chẽ hơn, hãy tải wheel và `SHA256SUMS` rồi kiểm checksum trước khi cài. Quy
trình copy-paste riêng cho macOS và Linux nằm trong
[docs/deployment.md](docs/deployment.md#verify-and-install-a-release-wheel); tải thành công không
đồng nghĩa đã xác minh checksum.

Extras cài từ cùng wheel release bằng distribution đúng:

```bash
python -m pip install \
  "phamviet-local-rag-mcp[local-embeddings] @ https://github.com/phamviet86/local-rag-mcp/releases/download/v0.9.0/phamviet_local_rag_mcp-0.9.0-py3-none-any.whl"
python -m pip install \
  "phamviet-local-rag-mcp[google-drive] @ https://github.com/phamviet86/local-rag-mcp/releases/download/v0.9.0/phamviet_local_rag_mcp-0.9.0-py3-none-any.whl"
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

Các artifact v0.8.0 trước đây chưa có `install-skills`; giữ nguyên bằng chứng và hướng dẫn cũ trong
[ghi chú v0.8.0](docs/releases/v0.8.0.md). Xem [ghi chú v0.9.0](docs/releases/v0.9.0.md) để nâng cấp.

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

See [Drive sync identity, retries, coverage and isolated trial](docs/drive-sync.md).
