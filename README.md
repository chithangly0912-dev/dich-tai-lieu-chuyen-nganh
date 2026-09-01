# Dịch PDF chuyên ngành sang Tiếng Việt

Ứng dụng web tự chạy (self-hosted): tải lên file PDF **tiếng Anh hoặc tiếng
Trung** (tự động phát hiện ngôn ngữ nguồn), dịch sang **tiếng Việt** bằng
**DeepSeek API**, và xuất ra file **PDF** cố gắng giữ nguyên bố cục gốc
(đoạn văn, bảng biểu, hình ảnh, vị trí văn bản) ở mức tốt nhất có thể.

## Kiến trúc

```
pdf-translate-app/
├── backend/              # FastAPI + PyMuPDF + DeepSeek client
│   ├── main.py           # API endpoints (upload, poll trạng thái, tải kết quả)
│   ├── pdf_translator.py # Lõi xử lý: trích xuất bố cục, dịch, dựng lại PDF
│   ├── deepseek_client.py# Gọi DeepSeek API (dịch theo lô để tăng tốc)
│   ├── lang_detect.py    # Tự phát hiện tiếng Anh / tiếng Trung
│   ├── fonts/            # Noto Sans (Regular/Bold) - hỗ trợ đầy đủ dấu tiếng Việt
│   ├── storage/           # Nơi lưu file upload + kết quả theo từng job (tự tạo khi chạy)
│   └── requirements.txt
├── frontend/
│   └── index.html        # Giao diện web (1 file, không cần build)
└── .env.example           # Mẫu cấu hình API key
```

**Cách hoạt động:**

1. Trích xuất từng khối văn bản trong PDF (đoạn văn, ô bảng, nhãn...) cùng
   vị trí (bounding box), cỡ chữ, màu chữ, độ đậm.
2. Tự động phát hiện ngôn ngữ nguồn (tiếng Anh hay tiếng Trung) dựa trên
   tỉ lệ ký tự Hán so với ký tự Latin.
3. Gửi các khối văn bản tới DeepSeek API để dịch sang tiếng Việt (gộp
   nhiều đoạn vào 1 lần gọi API để tăng tốc, có cơ chế tự chuyển sang dịch
   từng đoạn riêng lẻ nếu phản hồi gộp bị lỗi định dạng).
4. Với mỗi trang: chụp lại màu nền quanh từng khối văn bản, xoá (redact)
   văn bản gốc, giữ nguyên hình ảnh và đường kẻ bảng, rồi chèn văn bản
   tiếng Việt đã dịch vào đúng vị trí đó (tự động giảm cỡ chữ nếu bản dịch
   dài hơn bản gốc).

## Cài đặt

Yêu cầu Python 3.10+.

```bash
cd backend
python3 -m venv .venv
source .venv/bin/activate        # Windows: .venv\Scripts\activate
pip install -r requirements.txt
```

## Cấu hình DeepSeek API key

Lấy API key tại: https://platform.deepseek.com/api_keys

Sao chép `.env.example` thành `.env` (đặt trong thư mục `backend/` hoặc ở
thư mục gốc dự án đều được) và điền key:

```bash
cp ../.env.example .env
# rồi mở .env và điền DEEPSEEK_API_KEY=sk-...
```

Ngoài ra bạn cũng có thể **không** đặt sẵn key trên server, mà nhập trực
tiếp API key trong giao diện web (mục "Nhập DeepSeek API key riêng" khi
dịch) - tiện khi nhiều người dùng chung server nhưng mỗi người tự trả phí
API riêng.

## Chạy ứng dụng

```bash
cd backend
uvicorn main:app --host 0.0.0.0 --port 8000
```

Mở trình duyệt tại: http://localhost:8000

Chạy ở chế độ phát triển (tự reload khi sửa code):

```bash
uvicorn main:app --reload --port 8000
```

## Sử dụng

1. Kéo thả (hoặc chọn) file PDF tiếng Anh/tiếng Trung.
2. (Tuỳ chọn) Nhập API key/base URL/model riêng nếu không dùng cấu hình
   mặc định của server.
3. Bấm **"Bắt đầu dịch"** và theo dõi tiến trình theo thời gian thực.
4. Khi hoàn tất, bấm **"Tải file PDF đã dịch"**.

File PDF gốc và kết quả dịch được lưu tạm trong `backend/storage/<job_id>/`
để có thể tải lại; bạn có thể xoá thư mục này định kỳ để giải phóng dung
lượng đĩa (ứng dụng không tự động dọn dẹp).

## Giới hạn đã biết

Đây là công cụ "best-effort" giữ bố cục, **không phải** một trình dàn
trang (DTP) hoàn hảo. Một số giới hạn cần lưu ý:

- **PDF dạng ảnh scan** (không có lớp văn bản, chỉ là hình ảnh trang giấy)
  sẽ không trích xuất được văn bản để dịch. Cần chạy OCR trước (ví dụ
  `ocrmypdf`) để tạo lớp văn bản, rồi mới dùng công cụ này.
- **Layout nhiều cột phức tạp, văn bản xoay/dọc, công thức toán học dạng
  hình ảnh** có thể không được xử lý chính xác.
- **Bảng có màu nền phức tạp/hoạ tiết** - công cụ lấy mẫu màu nền quanh
  từng khối văn bản để tô lại cho khớp, nhưng có thể không hoàn hảo với
  nền có gradient hoặc hoạ tiết.
- **Độ dài văn bản tiếng Việt** thường dài hơn tiếng Anh/tiếng Trung
  20-40%. Công cụ tự giảm cỡ chữ để vừa khung gốc; với các trang dày đặc
  chữ, bản dịch có thể hiển thị nhỏ hơn đáng kể so với bản gốc.
- **Văn bản nằm trong hình ảnh** (ví dụ chữ được vẽ trực tiếp trong một
  hình minh hoạ) sẽ không được dịch, vì đây được coi là ảnh, không phải
  văn bản có thể trích xuất.
- Đây không phải bản dịch được con người kiểm duyệt - với tài liệu quan
  trọng (hợp đồng, hồ sơ pháp lý...), nên có bước rà soát của người dịch
  chuyên nghiệp trước khi sử dụng chính thức.

## Chi phí & tốc độ

Chi phí phụ thuộc vào bảng giá DeepSeek API hiện hành và độ dài tài liệu
(xem giá mới nhất tại https://platform.deepseek.com). Ứng dụng gộp nhiều
đoạn văn bản ngắn vào một lần gọi API (tối đa ~25 đoạn hoặc ~2500 ký tự
mỗi lần gọi) để giảm số lượt gọi và tăng tốc độ dịch cho các tài liệu
nhiều trang.

## Khắc phục sự cố

- **"Thiếu DEEPSEEK_API_KEY"**: đặt biến môi trường trong file `.env` hoặc
  nhập key trực tiếp trong giao diện web.
- **Lỗi HTTP 401/403 từ DeepSeek**: kiểm tra lại API key và số dư tài
  khoản DeepSeek.
- **Bản dịch bị lệch vị trí/tràn chữ nhẹ**: xảy ra với các trang có bố
  cục rất dày đặc; xem lại mục "Giới hạn đã biết" ở trên.
- **File quá lớn bị từ chối**: tăng biến môi trường `MAX_UPLOAD_MB` trong
  `.env`.
"# dich-tai-lieu-chuyen-nganh" 
