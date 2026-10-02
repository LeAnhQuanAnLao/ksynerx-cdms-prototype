# Bài học Kinh nghiệm, Ma trận Tính năng & Phân tích Kỹ thuật (Lessons Learned)

## 1. Bài học Kinh nghiệm về Mặt Kỹ thuật (Technical Lessons Learned)

### 1.1. Ngữ nghĩa Exactly-Once trong Hệ thống Nạp Dữ liệu Đa Kênh & Phân tán
- **Bản chất cốt lõi**: Trong thực tế mạng máy tính, việc truyền tải dữ liệu "chính xác một lần" (True Exactly-Once Delivery) qua ranh giới mạng là bất khả thi về mặt toán học do bài toán Hai vị tướng (Two Generals' Problem). Trong kỹ thuật phần mềm thực tế, ngữ nghĩa Exactly-Once được hiện thực hóa thông qua cơ chế: **Truyền nhận Ít nhất một lần (At-Least-Once Delivery) + Lọc trùng lặp Bất biến (Idempotent Deduplication)**.
- **Lọc trùng mức Payload vs. Lọc trùng mức Sự kiện**: Chỉ dựa vào `event_id` do client gửi là không đủ, bởi vì một hệ thống bên ngoài hoặc thao tác tải lên thủ công file Excel có thể gửi lại nguyên trạng thái tồn kho với một UUID hoàn toàn mới hoặc không kèm ID nào. Do đó, việc kết hợp:
  1. **Chuỗi băm nội dung tất định (Deterministic Content Hashing)**: Tính SHA-256 trên các trường dữ liệu nghiệp vụ đã được chuẩn hóa.
  2. **Thời gian nguồn tăng đơn điệu (Monotonic Source Timestamps)**: Từ chối các bản ghi có thời gian cũ hơn snapshot hiện tại (`t_incoming <= t_current`).
  3. **Sai khác mức thuộc tính (Field-Level Diffing)**: Chỉ ghi nhận delta khi thực sự có sự biến động số liệu.
  giúp đảm bảo tính toàn vẹn dữ liệu 100% ngay cả khi client gửi lại (retry), tải lại file hoặc rơi vào vòng lặp vô tận.

### 1.2. Kiểm soát Đồng thời Dưới Tải Đột biến (Concurrency Control Under Spike Loads)
- **Thách thức tải đột biến**: Khi tiến trình Polling định kỳ, Webhook thời gian thực và file Excel tải lên cùng lúc tác động vào cùng một khóa `(warehouse_code, partner_sku)`, hiện tượng tương tranh (Race Condition) có thể dẫn đến việc trùng lặp số phiên bản (`version`) hoặc mất mát dữ liệu cập nhật (Lost Update).
- **Quyết định thiết kế**: Áp dụng cơ chế **Khóa dòng bi quan (Pessimistic Row-level Locking)** với cú pháp `SELECT ... FOR UPDATE` giới hạn nghiêm ngặt theo phạm vi cặp khóa kho và SKU.
- **Kết quả**: Triệt tiêu hoàn toàn xung đột phiên bản, đồng thời vẫn cho phép các luồng xử lý song song trên các SKU *khác nhau* mà không gây nghẽn cổ chai toàn cục trên cơ sở dữ liệu.

### 1.3. Đánh đổi giữa 3 Cơ chế Nạp Dữ liệu (Trade-offs)
- **Polling Định kỳ (Scheduler)**:
  - *Ưu điểm*: Đơn giản, đảm bảo tính nhất quán cuối cùng (Eventual Consistency) ngay cả khi webhook bị mất kết nối mạng.
  - *Nhược điểm*: Tốn tài nguyên mạng và có độ trễ giữa các chu kỳ quét. Cần có mô hình **Circuit Breaker** và cơ chế dãn cách (Backoff) để không làm tê liệt dịch vụ nguồn khi họ gặp sự cố.
- **Webhooks (Real-Time Push)**:
  - *Ưu điểm*: Độ trễ gần như bằng 0 (Near-zero latency), phản ứng tức thì.
  - *Nhược điểm*: Dễ bị quá tải khi có lượng dữ liệu tăng vọt (burst), sự kiện có thể đến lệch thứ tự (out-of-order) và client có thể gửi trùng lặp khi mạng chập chờn.
- **Upload File Excel (Giao diện Vận hành / ERP)**:
  - *Ưu điểm*: Bắt buộc phải có để tích hợp hệ thống cũ (legacy) hoặc điều chỉnh kho hàng loạt từ con người.
  - *Nhược điểm*: Tiêu tốn bộ nhớ RAM khi phân tích file lớn. Được giải quyết bằng cơ chế đọc luồng (streaming) kết hợp giới hạn dung lượng/số dòng và chuẩn hóa tên cột linh hoạt.

---

### 1.4. Xung đột Khóa: Batch Idempotency vs. Item Event ID
- **Cạm bẫy thực tế**: Khi client gửi một Webhook chứa danh sách nhiều mặt hàng (batch) trong một request kèm theo một mã `event_id` duy nhất của cả lô, nếu gán trực tiếp `event_id` đó cho từng dòng sự kiện biến động sẽ gây lỗi vi phạm khóa duy nhất trong DB (`UNIQUE constraint failed: inventory_change_events.event_id`).
- **Giải pháp**: Tách bạch rõ ràng 2 khái niệm:
  1. Đăng ký `client_event_id` của cả lô vào bảng `processed_events_idempotency` để loại bỏ nhanh các request gửi lặp toàn bộ payload.
  2. Tạo mã định danh biến động tất định cho từng mặt hàng đơn lẻ: `f"{client_event_id}#{warehouse_code}#{partner_sku}#v{version}"`.

### 1.5. Bảo mật Đa Tầng cho API và Cổng Nạp Dữ liệu (Defense-in-Depth)
- **Chữ ký HMAC & Cửa sổ Chống Tấn công Phát lại (Anti-Replay Windows)**: Webhook đẩy qua mạng công cộng được xác minh tính toàn vẹn bằng HMAC-SHA256 (`X-CDMS-Signature`), kết hợp kiểm tra độ lệch thời gian của header `X-CDMS-Timestamp` (cửa sổ tối đa 300 giây) để loại bỏ nguy cơ Replay Attack.
- **Bảo vệ Upload File & Chống Formula Injection**: Ngoài việc xác thực chữ ký byte nhị phân (Magic Bytes `PK\x03\x04`) và giới hạn dung lượng/số dòng, các ô dữ liệu chuỗi được làm sạch để chống tấn công tiêm công thức mã độc vào Excel/CSV (DDE Injection) bằng cách tự động escape các tiền tố lệnh nguy hiểm (`=`, `+`, `-`, `@`).
- **Giới hạn Tần suất có Giới hạn Bộ nhớ (Memory-Bounded Rate Limiting)**: Thuật toán Sliding Window trong bộ nhớ tích hợp cơ chế tự động dọn dẹp (evict) các IP đã hết hạn, ngăn chặn nguy cơ kẻ tấn công giả mạo hàng triệu IP (IP-Spoofing) nhằm làm tràn bộ nhớ RAM máy chủ.

### 1.6. Xử lý Trùng SKU trong Cùng Một Batch & SQLAlchemy Identity Map
- **Cạm bẫy thực tế**: Khi trong một lô hàng có nhiều biến động cho *cùng một* SKU (ví dụ: tạo mới mặt hàng rồi ngay lập tức cập nhật số lượng trong cùng một file Excel hoặc webhook), việc truy vấn DB trong cùng một session có `autoflush=False` sẽ không tìm thấy bản ghi vừa thêm vào bộ nhớ đệm, dẫn đến việc cố INSERT lần 2 và gây lỗi `UNIQUE constraint failed`.
- **Giải pháp**: Gọi trực tiếp lệnh `self.db.flush()` ngay sau khi ghi nhận mỗi thay đổi. Nhờ đó, Identity Map của transaction được đồng bộ hóa tức thì, các thay đổi tiếp theo của cùng SKU trong lô sẽ tự động chuyển đổi mượt mà sang UPDATE hoặc IGNORED_DUPLICATE với version và diff chính xác tuyệt đối.

---

## 2. Ma trận Trạng thái Triển khai Tính năng (Feature Matrix)

### 2.1. Các Tính năng Đã Hoàn thành (Implemented Features)
| Tính năng | Trạng thái | Chi tiết triển khai |
| :--- | :---: | :--- |
| **Giả lập Dịch vụ Vietful (Mock Vietful)** | HOÀN THÀNH | Xây dựng REST API mock theo đúng chuẩn OpenAPI của Vietful (`/Products` và `/Products/inventories`), sinh dữ liệu bằng `Faker`. Hỗ trợ lọc gia tăng `FromDate` và API kiểm thử `/simulate-change`. |
| **Nạp Dữ liệu Đa Kênh (3 Cơ chế)** | HOÀN THÀNH | Hỗ trợ đầy đủ Polling theo chu kỳ (với phân trang và watermark), Webhook CDC thời gian thực (`/api/v1/cdc/webhook`), và REST Upload Excel (`/api/v1/cdc/upload-excel`). |
| **Lõi CDC Xử lý Exactly-Once** | HOÀN THÀNH | Lọc trùng bằng chuỗi băm SHA-256, kiểm tra thứ tự timestamp, tính toán sai khác chi tiết (JSON diff), chỉ lưu trữ các delta vào bảng `inventory_change_events`. |
| **Khóa Tương tranh Bi quan (Row Lock)** | HOÀN THÀNH | Khóa dòng theo cặp `(warehouse_code, partner_sku)` kết hợp vòng lặp retry có `expunge_all` giúp ngăn chặn triệt để xung đột phiên bản dưới tải đột biến. |
| **Bảo mật Chuyên nghiệp Nhiều Lớp** | HOÀN THÀNH | Xác thực chữ ký HMAC-SHA256, kiểm tra Anti-Replay Timestamp, xác thực API Key/Bearer, Giới hạn tần suất Sliding Window, kiểm tra Magic Bytes file Excel, chống Formula Injection và cấu hình CORS chặt chẽ. |
| **Khả năng Chịu lỗi (Resilience)** | HOÀN THÀNH | Mô hình Circuit Breaker cho Vietful; Cơ chế retry kết nối và tự động rollback cho Database; Khả năng phục hồi dữ liệu nguyên vẹn sau khi máy chủ khởi động lại. |
| **Xóa mềm & Đối chiếu Danh mục Kho** | HOÀN THÀNH | Ghi nhận delta `DELETE` khi có cờ `isActive = false`, cùng endpoint đối chiếu kho `/api/v1/cdc/reconcile` để tự động phát hiện các sản phẩm bị xóa khỏi danh mục nguồn. |
| **Khả năng Quan sát & Đo lường (Metrics)** | HOÀN THÀNH | Endpoint `/metrics` chuẩn định dạng Prometheus theo dõi thời gian uptime, trạng thái Circuit Breaker, số lượng snapshot hiện hành, tổng sự kiện delta theo từng loại và nguồn nạp. |
| **Client Giả lập Đầy đủ** | HOÀN THÀNH | `emulating_callback_client.py`, `rest_excel_client.py`, và kịch bản demo một chạm `run_demo.py` mô phỏng đầy đủ dữ liệu thực tế, trùng lặp, chữ ký HMAC và tải đột biến. |
| **Bộ Kiểm thử Tự động Toàn diện** | HOÀN THÀNH | 43 test cases đạt tỷ lệ PASS 100%, độ bao phủ mã nguồn 87% toàn hệ thống (Unit, Integration, Security, Metrics và Concurrency Spike Load thật). |
| **Đóng gói Docker Container Chuẩn hóa** | HOÀN THÀNH | Dockerfile đa tầng (Multi-stage build), chạy dưới quyền user không có quyền root (`appuser:10001`), tiến trình init `tini`, healthcheck liên tục, cô lập port DB (`127.0.0.1:5434`) và `docker-compose.yml`. |

### 2.2. Các Định hướng Mở rộng trong Tương lai (Considered Extensions / Roadmap)
| Tính năng | Cân nhắc & Trạng thái | Lý do & Bước tiếp theo |
| :--- | :---: | :--- |
| **Message Broker Phân tán (Kafka/RabbitMQ)** | Đã cân nhắc | Khi mở rộng ra hệ thống quy mô lớn hàng triệu sự kiện/giây trên cụm nhiều máy chủ, việc đặt một hàng đợi phân tán (Kafka) trước CDMS sẽ giúp tách biệt hoàn toàn tầng tiếp nhận và tầng ghi DB. Trong bản prototype, tính năng này được giữ ở mức đơn giản nhằm đáp ứng đúng yêu cầu chạy gọn gàng trên **một máy đơn lẻ (single machine)** mà không phụ thuộc hạ tầng nặng nề. |
| **Bộ nhớ đệm & Khóa phân tán Redis** | Đã cân nhắc | Hỗ trợ lọc trùng lặp siêu tốc trên RAM trước khi chạm vào PostgreSQL. Đối với phiên bản máy đơn hiện tại, khóa dòng PostgreSQL và thuật toán Sliding Window trong bộ nhớ đã đảm bảo trọn vẹn tính toàn vẹn ACID mà không phát sinh thêm chi phí đồng bộ cache. |

---

## 3. Báo cáo Minh bạch về Phân công Công việc & Sử dụng AI (AI Disclosure)

Tuân thủ đúng yêu cầu đầu ra của bài kiểm tra tuyển dụng:
*"Clearly explain which code is written by the candidate and by using AI tools."*

### 3.1. Phần Công việc do Ứng viên Trực tiếp Thực hiện
- Phân tích yêu cầu nghiệp vụ từ tài liệu `FullStack_BàiTest.pdf`, thiết kế kiến trúc hệ thống tổng thể và mô hình hóa dữ liệu (Data Modeling).
- Thiết kế giải thuật cốt lõi Exactly-Once (băm SHA-256 + kiểm tra tính đơn điệu của timestamp + tính toán granular diff + cơ chế khóa dòng bi quan).
- Phát hiện, phân tích và đưa ra giải pháp xử lý xung đột khóa trong lô hàng (Batch Idempotency vs. Item Event ID, cơ chế `self.db.flush()` cho Identity Map).
- Xây dựng kiến trúc bảo mật đa tầng (HMAC, Anti-Replay window, Magic bytes, Formula injection sanitization, Circuit breaker).
- Thiết kế các kịch bản kiểm thử tải đột biến và xác thực kết quả toàn hệ thống.

### 3.2. Phần Công việc do Công cụ AI (Antigravity / Gemini) Hỗ trợ
- Tạo khung mã nguồn mẫu (boilerplate code) tuân thủ nghiêm ngặt các quy tắc kiến trúc module hóa (< 250 dòng/file).
- Ánh xạ nhanh chóng các lớp DTO/Pydantic schemas bám sát tài liệu đặc tả OpenAPI của Vietful.
- Sinh tập dữ liệu mẫu giả lập tồn kho kho hàng Việt Nam bằng thư viện `Faker`.
- Hỗ trợ vẽ các sơ đồ kiến trúc hệ thống bằng cú pháp Mermaid và tổng hợp nhanh tài liệu báo cáo kỹ thuật.
