# Phác thảo: dịch vụ RAG độc lập cho AI agent

Trạng thái: bản phác thảo để thảo luận (2026-11-06). Chưa có mã nào được thay đổi theo tài liệu này.
Mọi tham chiếu `file:dòng` là theo commit `ad5a5bd`.

## Trạng thái triển khai (2026-11-07)

Đã làm trên nhánh `feat/agent-rag-service` (chi tiết trong `CHANGELOG.md`, mục `[Unreleased]`):
daemon `serve` làm executor và heartbeat, lịch đồng bộ theo source, `refresh`, độ mới dữ liệu,
service account và Google Docs xuất Markdown, phân biệt thùng rác với xoá hẳn, `list_documents`,
giới hạn `read`, hướng dẫn VPS qua SSH, reranker tuỳ chọn, MRR/nDCG và baseline eval, tiền tố ngữ
cảnh cho chunk cửa sổ, định dạng HTML/CSV/JSON/EPUB/ODF, ảnh trang PDF cho Drive, cờ bố cục, gộp
workflow release, deprecate alias `local-rag`, setting `legal_chunking`.

Chưa làm (hoãn): HTTP công khai/OAuth, Docling, log truy vấn, xử lý bảng tính chuyên biệt, và thử
thực tế Drive/VPS (người dùng tự kiểm theo `docs/deployment-vps.md` và `docs/setup.md`).

## 1. Mục tiêu

| # | Mong muốn | Tóm tắt hiện trạng |
| --- | --- | --- |
| 1 | Chỉ cần thả file vào Google Drive là được RAG | Có adapter Drive, nhưng cần OAuth token file và chỉ tự chạy khi `serve` đang chạy |
| 2 | Tự quét file mới, file sửa, file xoá (định kỳ + khi agent yêu cầu) | Có: `changes.list` + reconcile định kỳ 600 s, nhưng chỉ trong tiến trình `serve`; quét chủ động chỉ cho admin |
| 3 | Chạy trên VPS dùng chung (SSH + stdio) hoặc cài riêng trên máy cá nhân | Có stdio (`mcp.py:1014`); chưa có hướng dẫn SSH, job vẫn chạy trong tiến trình gọi |
| 4 | Nhiều source: local files + Google Drive (không làm ổ mạng) | Có local + Drive |
| 5 | Search ngữ nghĩa + toàn văn, đọc toàn bộ tài liệu khi cần | Có hybrid FTS5 + vector + RRF, `read`, `get_chunk`; thiếu rerank và thiếu tool duyệt danh sách |
| 6 | Xử lý các định dạng phổ biến (chưa cần ảnh, âm thanh) | PDF, DOCX, XLSX, PPTX, TXT, MD; thiếu HTML, CSV, EPUB, ODF, RTF, `.doc/.xls` cũ |

Phạm vi: một operator, một index, các agent tin cậy. Không làm phân quyền theo từng người dùng (ACL)
ở giai đoạn này; ghi rõ giới hạn này trong tài liệu triển khai.

## 2. Những gì học được từ bên ngoài

Nguồn chính (số liệu do chính tác giả công bố):

- **Anthropic – Contextual Retrieval** ([bài viết](https://www.anthropic.com/research/contextual-retrieval)):
  thêm 50–100 token ngữ cảnh vào đầu mỗi chunk trước khi đưa vào BM25 và embedding. Embedding + BM25
  có ngữ cảnh giảm tỉ lệ truy xuất hỏng ở top-20 từ 5,7 % xuống 2,9 %; thêm reranker xuống 1,9 %
  (giảm 67 %). Các kỹ thuật cộng dồn được với nhau.
- **Google Drive API**: `changes.list`/`files.list` tốn 100 đơn vị quota, tải/export tốn 200 đơn vị
  ([limits](https://developers.google.com/workspace/drive/api/guides/limits)); Google Docs export được
  sang `text/markdown`, Sheets sang XLSX/CSV, Slides sang PPTX/text
  ([export formats](https://developers.google.com/workspace/drive/api/guides/ref-export-formats)).
  `files.export` giới hạn 10 MB đầu ra (theo một nguồn thứ ba, cần kiểm chứng khi làm).
- **MCP spec**: stdio dùng thông tin xác thực từ môi trường; HTTP (Streamable HTTP) nên dùng OAuth 2.1
  với Protected Resource Metadata ([authorization](https://modelcontextprotocol.io/docs/2026-07-28/tutorials/security/authorization)).
  Với agent nội bộ, bearer token cấu hình tay là cách phổ biến.

Nguồn thứ cấp (bài blog, so sánh của nhà cung cấp; chỉ dùng như định hướng):

- Kiến trúc mặc định năm 2026: hybrid BM25 + dense, hợp nhất RRF, rồi cross-encoder rerank trên
  top 30–50; tuỳ chọn GraphRAG cho câu hỏi liên tài liệu.
- Agent coding (Claude Code) dùng grep + đọc nguyên file thay vì vector. Bài học: agent cần **công
  cụ duyệt và đọc** (liệt kê, mở toàn bộ), không chỉ một ô tìm kiếm; với văn bản ngôn ngữ tự nhiên
  thì hybrid vẫn cần.
- Onyx (MIT, 40+ connector, đồng bộ tăng dần, ACL; khoảng 11 container) và RAGFlow (DeepDoc, cần
  khoảng 16 GB RAM) là tham chiếu mẫu thiết kế, không phải thứ để thay thế dự án này.
- Bộ đọc tài liệu: Docling (MIT, chạy CPU, giữ cấu trúc và trang) hợp với RAG; MinerU mạnh về bảng,
  công thức, CJK; markitdown nhẹ, nhiều định dạng nhưng ra Markdown phẳng. Chưa ai đo trên tiếng Việt.

## 3. Kiến trúc đề xuất

```
                       ┌──────────────────────── Agent / client ───────────────────────┐
                       │  Claude Code / Desktop (stdio)   claude.ai, agent từ xa (HTTP) │
                       └───────────────┬───────────────────────────────┬───────────────┘
                                       │ stdio                         │ Streamable HTTP
                                       │                               │ + bearer token
                       ┌───────────────▼───────────────────────────────▼───────────────┐
  Query frontend       │  MCP tools: search · list_documents · read · get_chunk ·      │
  (không giữ trạng     │  index_coverage · status · doctor   (reader mặc định)          │
   thái, nhiều bản)    └───────────────────────────────┬───────────────────────────────┘
                                                       │ đọc
                       ┌───────────────────────────────▼───────────────────────────────┐
  Lưu trữ              │  SQLite: documents · chunks · chunks_fts (FTS5) · vectors ·    │
                       │  source_sync_state · jobs · reviews       + raw cache          │
                       └───────────────────────────────▲───────────────────────────────┘
                                                       │ ghi (chỉ qua job queue)
                       ┌───────────────────────────────┴───────────────────────────────┐
  Sync daemon          │  Scheduler: mỗi source một chu kỳ riêng                        │
  (một tiến trình,     │  SourceAdapter: local (watchdog + quét) · drive (changes.list) │
   chạy dài)           │                                                                │
                       │  Extractor theo media type → SourceSpan → chunk → FTS/embedding│
                       └────────────────────────────────────────────────────────────────┘
```

Nguyên tắc:

- Tách **sync daemon** (quét định kỳ) khỏi **query frontend** (đọc). Frontend stdio và HTTP dùng
  chung DB.
- Mọi thay đổi index đi qua job queue hiện có. SQLite chạy WAL (`db.py:221`) và `jobs.py` có
  `index_writer_lease` với heartbeat, nên frontend admin và daemon không ghi chồng lên nhau.
- Giữ SQLite/FTS5: đủ cho quy mô một operator, không thêm dịch vụ ngoài.
- Thiếu embedding hoặc OCR vẫn chạy (FTS), như hiện tại.
- **OCR giữ nguyên**: chạy cục bộ trên máy dịch vụ (PP-OCRv6 qua ONNX Runtime + PDFium,
  `ocr_runtime.py`; `ocr_mode: full | no-ocr`), không gọi API ngoài. Có bản cho Linux x64/arm64,
  macOS arm64, Windows x64, nên chạy được trên VPS. PDF scan từ Drive được tải về raw cache rồi OCR
  như file local. Trang OCR vẫn vào hàng review và được đánh dấu `ocr` khi search.

## 4. Thiết kế theo từng mong muốn

### 4.1 Thả file vào Drive là được RAG

Hiện có:

- `DriveAdapter` với `changes.list`, cursor lấy trước khi duyệt cây, hỗ trợ Shared Drive
  (`drive.py:185–218, 256–348`).
- Xác thực chỉ bằng OAuth token file, bắt buộc quyền 0600 (`drive.py:171`). Không có service account
  (grep `service_account` không thấy gì).

Thiếu:

- Trên VPS không có trình duyệt, OAuth người dùng khó thiết lập và token có thể hết hạn.

Đề xuất:

1. Thêm **service account**. Người dùng chỉ cần chia sẻ thư mục Drive (quyền xem) với email của
   service account, và mọi file trong đó tự được index. Giữ OAuth token như lựa chọn thứ hai.
2. Export Google Docs sang **`text/markdown`** thay vì `text/plain` (`drive.py:26`). Bộ chia chunk
   theo cấu trúc đã hỗ trợ Markdown (`indexer.py:914`), nên heading của Docs sẽ thành section.
3. Lỗi export/tải đã được ghi theo từng file vào coverage với stage `download_export`
   (`docs/drive-sync.md`). Chỉ cần kiểm tra thêm: file vượt giới hạn export có lý do rõ ràng không.

### 4.2 Tự quét thay đổi, file mới, file xoá

Hiện có:

- `MultiSourceWatchService.run` (`watcher.py:183`) reconcile toàn bộ source mỗi `reconcile_seconds`
  (mặc định 600, `config.py:62`) và lắng nghe sự kiện file cho source local.
- Xoá/di chuyển trên Drive được xử lý qua `changes.list` (`drive.py:336`). Quét dở thì không suy ra
  xoá (`docs/drive-sync.md`).

Thiếu:

- Vòng quét này **chỉ chạy trong lệnh `serve`** (`cli.py:406–410`). Server MCP stdio (`cli.py:413`)
  không tự quét, nên cấu hình chỉ có MCP thì file mới trên Drive không được index cho đến khi có người
  chạy `reconcile`.
- Một chu kỳ chung cho mọi source.

- Gọi quét chủ động chỉ có qua `start_reconcile`/`reconcile`, là tool admin (`mcp.py:257, 280`).
  Agent reader không yêu cầu cập nhật được.

Mục tiêu: người dùng làm việc trên Drive như bình thường (tạo, sửa, xoá, di chuyển file). Họ
không phải làm gì thêm cho RAG; dịch vụ tự theo kịp.

Đề xuất:

1. **Quét định kỳ, luôn chạy cùng dịch vụ.**
   - Drive: mặc định mỗi **5 phút** gọi `changes.list` từ cursor đã lưu. Mỗi lần gọi chỉ hỏi
     "có gì thay đổi" (100 đơn vị quota), không duyệt lại cây; chỉ file thay đổi mới bị tải hoặc
     export lại. 5 phút là cân bằng giữa độ mới và quota; đặt được thấp nhất 60 s.
   - Local: giữ watcher thời gian thực như hiện tại.
   - Mọi source: **đối chiếu đầy đủ mỗi 12 giờ** để bắt thay đổi bị sót (cursor hỏng, mất mạng
     lâu, quyền chia sẻ đổi).
   - Chu kỳ đặt riêng cho từng source (`poll_seconds`, `full_reconcile_seconds` trong config
     source), thay cho `reconcile_seconds` chung.
   - Vòng quét chạy trong tiến trình dịch vụ (`serve`, hoặc server HTTP ở 4.3). Server stdio
     không tự quét, để nhiều client stdio không cùng gọi Drive. Writer lease đảm bảo mỗi lúc chỉ
     một tiến trình ghi.
2. **Gọi quét chủ động khi cần.**
   - Thêm tool `refresh(source?)` cho agent: xếp một job quét thay đổi (incremental) vào hàng đợi
     và trả `job_id`. Dùng khi người dùng vừa sửa file và muốn hỏi ngay, hoặc khi `status` cho thấy
     lần quét gần nhất đã lâu.
   - Giới hạn: chỉ quét thay đổi (không reindex, không xoá source, không quét đầy đủ); mỗi source
     tối đa 1 lần mỗi 60 s; nếu đã có job đang chạy thì gộp vào job đó.
   - CLI tương ứng: `local-rag-mcp refresh [--source NAME]` (dùng lại `reconcile` hiện có).
   - Endpoint HTTP một người dùng (4.3) có đủ tool nên `refresh` luôn dùng được. Với profile
     `reader` cũ, `refresh` là ngoại lệ của quy tắc "Reader tools do not mutate state" (nó chỉ thêm
     job vào hàng đợi); cập nhật `AGENTS.md` cho khớp.
3. **Cho agent biết dữ liệu mới đến đâu.**
   - `status`, `sources` và `search.coverage` trả `last_synced_at` và `next_sync_at` của từng
     source.
   - `doctor` cảnh báo khi một source quá 3 chu kỳ chưa quét thành công (mất token, mất mạng, hết
     quota), hoặc không có tiến trình dịch vụ nào chạy (dựa vào heartbeat ghi trong DB).
4. Không dùng `changes.watch` (push): nó cần một địa chỉ HTTPS công khai, trái với hướng chạy cục bộ.
   Có thể xem lại khi đã có chế độ HTTP ở 4.3.
5. **Thùng rác và xoá hẳn trên Drive.**
   - Hiện tại: `changes.list` coi `trashed` và `removed` như nhau, đều xoá ngay khỏi index
     (`drive.py:336`, `drive.py:548–551`). Quét lại cây dùng `trashed=false` (`drive.py:207`) nên
     file trong thùng rác cũng bị coi là đã xoá. File raw trong `cache/sources/<id>/raw/` của file
     bị xoá **không được dọn** (`_cleanup_raw` chỉ chạy khi index thành công, `drive.py:595`).
   - **Vào thùng rác** (`trashed=true`): giữ nguyên document, chunk, vector và raw cache, đặt
     `state=trashed`. Không xuất hiện trong `search`, `list_documents`, coverage mặc định;
     `read`/`get_chunk` theo `stable_ref` trả lỗi rõ "file đang ở thùng rác" (có cờ
     `include_trashed` nếu cần xem). Không cần tải hay embedding lại.
   - **Khôi phục từ thùng rác**: `changes.list` báo `trashed=false` → đặt lại `state=active`. Nếu
     nội dung đổi trong lúc đó (checksum/modified time khác) thì index lại như file sửa.
   - **Xoá hẳn** (`removed=true`, hoặc Drive tự xoá sau 30 ngày trong thùng rác): xoá document,
     chunk, vector, raw cache và cache trích xuất của file đó. Cũng áp dụng khi file bị mất quyền
     truy cập (bỏ chia sẻ), vì Drive báo cùng là `removed`.
   - Quét lại cây: liệt kê cả file `trashed=true` để phân biệt "trong thùng rác" với "đã mất".
   - Dọn rác định kỳ: lượt đối chiếu đầy đủ (12 giờ) xoá file raw/cache không còn document nào trỏ
     tới, để dọn phần còn sót từ trước.
   - Source local: không có thùng rác trong source (đưa vào thùng rác của hệ điều hành là file rời
     khỏi thư mục), nên file mất khỏi đĩa thì xoá luôn index và cache, như hiện nay.

### 4.3 Dịch vụ độc lập: VPS dùng chung (SSH + stdio) hoặc cài riêng

Hiện có:

- Server MCP SDK chỉ chạy stdio (`mcp.py:1014`). Có 3 profile reader/reviewer/admin
  (`mcp.py:324–357`), nhưng `docs/agents.md` ghi rõ profile **không phải** cơ chế xác thực.
- SDK `mcp==2.0.0` đang cài đã hỗ trợ `run(transport="streamable-http", host, port, …)` và có
  `TokenVerifier`, nên không cần thêm thư viện mới.

Mục tiêu (chốt 2026-11-07): chỉ có **hai cách triển khai**, không chạy qua LAN/mạng nội bộ:

| Cách | Ai dùng | Kết nối |
| --- | --- | --- |
| **A. VPS dùng chung** | Mọi máy của người dùng cùng một cơ sở RAG | **SSH + stdio** (chính); HTTP chỉ làm sau, khi cần connector claude.ai/di động |
| **B. Cài riêng trên máy cá nhân** | Một máy, index riêng | stdio cục bộ như hiện nay |

Đề xuất:

1. **Cách A, kết nối chính: SSH + stdio.**
   - Trên VPS: `local-rag-mcp serve` chạy nền (systemd do operator tự cài) để quét Drive/local
     định kỳ và **thực thi job trong hàng đợi**.
   - Trên máy client, cấu hình MCP chạy lệnh qua SSH, ví dụ:
     `ssh -T vps /home/<user>/.local/share/local-rag-mcp/.venv/bin/local-rag-mcp-server`.
     Mỗi phiên client mở một tiến trình stdio trên VPS, cùng đọc một SQLite (WAL).
   - Vì sao chọn cách này:
     - Không mở port, không cần token hay TLS. SSH key lo xác thực và mã hoá.
     - Không phải làm kiểm Origin hay OAuth.
     - Dùng lại server stdio đã kiểm chứng; gần như không phải viết code transport mới.
   - Giới hạn: chỉ dùng được với client chạy được lệnh cục bộ (Claude Code, Claude Desktop,
     Codex, Cursor…). claude.ai web và app di động không chạy được lệnh SSH.
   - Yêu cầu khi cài:
     - SSH key không hỏi passphrase mỗi lần, dùng ssh-agent hoặc keychain.
     - Lệnh SSH dùng đường dẫn tuyệt đối, vì SSH không tương tác không nạp PATH.
     - API key embedding đặt trong config được bảo vệ trên VPS, không lấy từ shell của client.
     - Có thể dùng `ControlMaster`/`ControlPersist` để mở phiên nhanh hơn.
2. **Job phải do daemon chạy, không do tiến trình stdio.** Hiện `start_index_job` chạy job trong
   chính tiến trình đã gọi (`service.py:668–681`). Qua SSH, phiên bị ngắt thì job chết giữa chừng;
   writer heartbeat hết hạn sẽ đánh dấu lỗi (`jobs.py:35`), nhưng phải chạy lại.
   - Đổi lại: khi có daemon `serve`, `refresh`/`start_reconcile` từ stdio chỉ **xếp hàng** rồi trả
     `job_id`. Daemon lấy job ra chạy.
   - Không có daemon (cách B, chạy tay) thì giữ hành vi cũ.
   - `doctor` báo rõ "không có daemon, job sẽ chạy trong phiên này".
3. **Cách A, phương án phụ: SSH tunnel tới HTTP localhost.** `serve --http --host 127.0.0.1` trên VPS,
   client chạy `ssh -N -L 8765:127.0.0.1:8765 vps` rồi trỏ MCP tới `http://127.0.0.1:8765/mcp`.
   - Bảo vệ Origin của SDK tự bật khi chạy localhost.
   - Chỉ dùng khi client hỗ trợ HTTP mà không chạy lệnh được; phải giữ tunnel mở. Không ưu tiên.
4. **HTTP công khai (để sau).** Chỉ làm khi cần connector claude.ai hoặc app di động:
   - Bắt buộc TLS (Caddy/nginx), kiểm Origin (4.3.1).
   - OAuth 2.1 + RFC 9728, vì connector claude.ai không dùng bearer token tĩnh.
5. **Cách B, cài riêng.** Giữ nguyên stdio cục bộ: `serve` (tuỳ chọn) để tự quét, server stdio cho
   client. Index riêng, không đồng bộ với VPS.
6. **Embedding API giữ nguyên.**
   - Vẫn dùng `embedding_provider: openai` với API tương thích OpenAI (mặc định OpenRouter,
     `config.py:63–65`), hoặc `local`, hoặc `none` (chỉ FTS).
   - Với cách A, embedding và OCR đều chạy trên VPS; máy client không cần key hay model.
   - Thiếu key hoặc API lỗi thì search tự rơi về FTS như hiện nay.
7. **Một profile.** Không tách reader/reviewer/admin cho người dùng. Giữ cơ chế profile trong code để
   không phá cấu hình cũ; cấu hình SSH mặc định dùng đủ tool. Câu hỏi "reader được `refresh`
   không" ở 4.2 không còn quan trọng.

#### 4.3.1 Đối chiếu với spec MCP 2026-07-28 (trao đổi với phiên input-programming, 2026-11-07)

Đã kiểm chứng trong SDK `mcp==2.0.0` cài ở `.venv`.

Áp dụng ngay (cả SSH + stdio):

1. **Việc chạy lâu: giữ `job_id` + `job_status`.** Tasks giờ là extension riêng (SEP-2663), chưa rõ
   Claude Code/Desktop hỗ trợ chưa. Xem lại khi client hỗ trợ.
2. **Giới hạn kích thước kết quả.** Claude Code mặc định giới hạn khoảng 25.000 token mỗi kết quả
   tool. `read` cho phép tới 100.000 ký tự (`mcp.py:82–84`), sát ngưỡng. Hạ mặc định `length` (ví dụ
   20.000 ký tự) và luôn trả `end`/`total_chars` để đọc tiếp.
3. **`list_documents` dùng cursor** (`cursor` vào, `next_cursor` ra). Mọi thời gian (`last_synced_at`,
   `next_sync_at`) theo UTC ISO 8601.
4. **Không đổi `read` sang MCP resources.** Resources do ứng dụng chọn, không phải model tự gọi; giữ
   tool `read`. Có thể thêm `resource_link` trong kết quả search sau, khi đã thử với client.
5. **Sau khi thêm tool mới** (`refresh`, `list_documents`), client chỉ thấy khi mở phiên mới.
6. **stdio không dùng OAuth**, lấy thông tin từ môi trường (đúng spec). Qua SSH, xác thực là SSH key.

Chỉ áp dụng khi làm HTTP (phương án phụ hoặc sau này):

7. **Kiểm Origin/Host (bắt buộc theo spec).** Streamable HTTP MUST kiểm `Origin`, sai trả 403.
   - SDK chỉ tự bật bảo vệ khi `host` là `127.0.0.1`/`localhost`/`::1`
     (`mcp/server/lowlevel/server.py:739`).
   - Bind IP khác thì mặc định **tắt** (`transport_security.py:41`). Khi đó phải truyền
     `TransportSecuritySettings` với `allowed_hosts`/`allowed_origins`.
8. **Stateless HTTP** (`stateless_http=True`). Spec 2026-07-28 bỏ session cấp giao thức.
9. **Huỷ request.** Client ngắt kết nối là tín hiệu huỷ; search/read nặng nên dừng.
10. **Auth.**
    - Bearer token tĩnh hợp lệ (authorization là OPTIONAL), so sánh constant-time. Đã chốt dùng
      một token, không có token admin riêng.
    - Connector claude.ai cần OAuth 2.1 + RFC 9728 (401 kèm `WWW-Authenticate resource_metadata`).

Repo đã làm tốt hơn kiến thức của input-programming và đã báo lại:
- Thương lượng cả 2026-07-28 lẫn 2025-11-25.
- Job queue bền + writer lease.
- Coverage trong mọi kết quả search.
- `stable_ref` + offset dùng lại được trong `read`.
- Cấu hình bảo vệ Origin của SDK Python.

### 4.4 Nhiều source: local files + Google Drive

Hiện có:

- `MultiSourceRAG` (`service.py:230`) chạy nhiều source chung một SQLite; chọn adapter theo
  `record.kind` (`service.py:469–488`).

Thiếu:

- Hai tính năng chỉ chạy với source local:
  - `get_page_image` từ chối Drive với thông báo "Drive files are not cached" (`service.py:48`),
    trong khi `drive.py:672` vẫn lưu bản gốc vào `cache/sources/<id>/raw/`.
  - `check_status` bỏ qua kiểm tra toàn vẹn của Drive (`validity.py:149`).
- Chưa có giao diện adapter chung giữa local và Drive.

Ngoài phạm vi (chốt 2026-11-07): ổ mạng (SMB/NFS), S3, WebDAV, OneDrive. Chỉ hỗ trợ local files và
Google Drive.

Đề xuất:

1. Cho `get_page_image` đọc từ raw cache của Drive, kèm cờ cho biết bản cache có còn khớp hash đã
   index không.
2. Kiểm tra toàn vẹn Drive bằng hash của raw cache, hoặc so `md5Checksum`/`version` mà `drive.py`
   đã lấy (`drive.py:210, 248`).
3. Rút ra interface `SourceAdapter` (liệt kê, thay đổi kể từ cursor, tải nội dung, metadata), lấy
   `DriveAdapter` làm mẫu.

### 4.5 Search ngữ nghĩa + toàn văn, đọc toàn bộ tài liệu

Hiện có:

- Hybrid FTS5 + vector, RRF k=60 (`search.py:391, 722`), đa dạng hoá theo tài liệu (`search.py:400`).
- `match=auto`, lọc theo `doc_type`, `valid_on`, `doc_no`.
- `read` có `start/length/around/window` (`mcp.py:72–86`). `get_chunk` có `neighbors` tối đa 3
  (`mcp.py:90–104`).
- Dòng ngữ cảnh `số hiệu | tên | Chương | Điều` được ghép vào text của chunk (`legal.py:371, 423`)
  rồi đưa vào `chunks_fts` (`indexer.py:537`). Đây là dạng contextual BM25 theo luật, không cần LLM.
  Ngoài ra `chunks_fts` còn index `title` và `relative_path` cho **mọi** chunk, kể cả chunk cửa sổ
  cố định, nên phía toàn văn đã có một phần ngữ cảnh.
- `read` trả về `end` và `total_chars`, nên agent đã đọc tiếp được đến hết tài liệu.
- Eval: `eval` báo recall@1/3/10. Trên 24 câu hỏi, hybrid đạt 0,79/0,92/0,96 (CHANGELOG).

Thiếu và đề xuất:

1. **Reranker tuỳ chọn.** Đặt sau `_combine` và **trước** `_diversify` (`search.py:105–108`), chấm
   lại khoảng 30–50 ứng viên. Model phải đa ngôn ngữ (loại bge-reranker-v2-m3) hoặc gọi API. Đóng
   gói như extra giống `local-embeddings`; không cấu hình thì hành vi như cũ.
2. **Ngữ cảnh cho embedding.** Khoảng trống thật nằm ở phía vector: chunk cửa sổ cố định được
   embedding mà không có tên tài liệu hay đường dẫn heading. Thêm các thông tin này vào text đưa đi
   embedding. Tuỳ chọn sau: sinh ngữ cảnh bằng LLM (contextual retrieval đầy đủ), có cache.
3. **Tool `list_documents`** (reader): liệt kê tài liệu theo source/folder/loại/ngày sửa, có phân
   trang. Hiện chưa có tool nào cho agent duyệt danh sách; `metadata` cần biết trước đường dẫn, còn
   `index_coverage` chỉ báo các file có vấn đề.
4. **Eval lớn hơn** và chạy trong CI trên dữ liệu mẫu; thêm MRR/nDCG. Mọi thay đổi ở mục này đều phải
   đo trước và sau.

### 4.6 Các định dạng phổ biến

Hiện có: `.txt .md .docx .xlsx .pptx .pdf` (`extract.py:38–50`); Drive thêm Docs/Sheets/Slides qua
export (`drive.py:25–44`).

Đề xuất:

1. Interface extractor theo media type, bắt buộc trả về `SourceSpan` có locator (trang, sheet, ô,
   slide, heading) để giữ citation.
2. Bổ sung các định dạng đọc được bằng thư viện chuẩn hoặc nhẹ trước: HTML, CSV, JSON, EPUB,
   ODT/ODS/ODP.
3. Extra tuỳ chọn cho định dạng khó: Docling (PDF phức tạp, `.doc`/`.rtf` qua chuyển đổi) hoặc
   markitdown (định dạng cũ). Chưa có số đo trên tiếng Việt, nên phải chạy eval trước khi dùng mặc định.
4. Ảnh và âm thanh: ngoài phạm vi.

## 5. Giữ, tăng cường, bỏ

**Giữ**

- SQLite/FTS5, job queue, coverage và cảnh báo stale/unindexed, quy tắc không lộ secret.
- Chunk theo cấu trúc và cite key.
- Tính năng pháp lý (`legal.py`, `validity.py`), nhưng tách thành **profile chunking/validity cắm
  thêm** thay vì nằm trong lõi.

**Tăng cường**: các mục ở phần 4.

**Ứng viên cần bạn quyết định bỏ hoặc gộp**

| Ứng viên | Bằng chứng | Đề xuất |
| --- | --- | --- |
| 4 workflow release theo từng phiên bản | `.github/workflows/release-v0.8.0.yml` … `release-v0.10.1.yml` | Gộp thành một workflow chạy theo tag |
| Lệnh cũ `local-rag` | `pyproject.toml` `[project.scripts]` | Báo deprecated một bản, rồi bỏ |
| Review `complex_layout` | 247/247 review đang mở đều là loại này; `doctor` luôn báo `degraded` | Chuyển thành cờ metadata, không đưa vào hàng đợi review |
| Lớp `LocalRAG` đơn source (`service.py:70`) và `WatchService` đơn source (`watcher.py:135`) | Đường chạy thật dùng `MultiSourceRAG` (`cli.py:410`), nhưng `LocalRAG` vẫn được `mcp.py:1019` và nhiều test dùng | Ưu tiên thấp: chỉ gộp sau khi đã chuyển test sang `MultiSourceRAG` |

## 6. Lộ trình

| Giai đoạn | Nội dung | Kết quả kiểm chứng |
| --- | --- | --- |
| 0. Đo | Chạy eval baseline trên index hiện có; mở rộng bộ câu hỏi | Bảng số recall/MRR làm mốc |
| 1. Truy xuất | Reranker tuỳ chọn, ngữ cảnh cho embedding, `list_documents` | Eval tăng so với mốc, không thoái lui |
| 2. Drive tự động | Service account, export Markdown, quét Drive 5 phút + đối chiếu đầy đủ 12 giờ, tool `refresh`, `last_synced_at`, heartbeat trong `doctor` | Sửa/thêm/xoá file trên Drive thì search phản ánh sau ≤ 5 phút, hoặc ngay sau `refresh` |
| 3. VPS dùng chung | Daemon `serve` thực thi job từ hàng đợi; hướng dẫn SSH + stdio (systemd trên VPS, cấu hình client); hạ mặc định `read` | Laptop và PC qua SSH cùng search/read/refresh trên một DB; ngắt SSH không làm hỏng job |
| 4. Mở rộng | Ảnh trang và kiểm tra toàn vẹn cho Drive, `SourceAdapter`, định dạng mới, extractor tuỳ chọn; HTTP + OAuth chỉ khi cần connector claude.ai | Test cho từng adapter/định dạng |
| 5. Dọn dẹp | Các mục ở bảng "ứng viên" mà bạn đồng ý | CI xanh, CHANGELOG ghi rõ |

## 7. Điểm chưa chắc chắn

- Chưa có nguồn Drive thật trong index hiện tại, nên các nhận định về Drive dựa trên đọc code.
- Giới hạn 10 MB của `files.export` lấy từ nguồn thứ ba.
- Chất lượng reranker, Docling, markitdown trên tiếng Việt chưa được đo.
- Server MCP qua HTTP chưa được thử với `mcp==2.0.0` trong dự án này; mới xác nhận SDK có API
  `run(transport="streamable-http")` và `TokenVerifier`.
- Service account + `changes.list`: tài liệu của `changes.list` nói `restrictToMyDrive` bỏ qua
  "shared files which have not been added to My Drive", nhưng không nói rõ feed mặc định có chứa
  file được chia sẻ cho service account hay không. Cần thử thật trước giai đoạn 2. Nếu không có, vẫn
  còn đường quét lại cây thư mục theo chu kỳ (đã có trong `drive.py`).
