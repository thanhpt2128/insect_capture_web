# AGENTS.md

## Project goal
Dự án này dùng để thu thập và xử lý dữ liệu radar AWR1843 realtime.
Ưu tiên: ổn định, dễ debug, không mất frame, code rõ ràng.

## General rules
- Không sửa ngoài phạm vi task.
- Không đổi public API nếu không được yêu cầu.
- Giữ diff nhỏ nhất có thể.
- Không format lại toàn bộ file.
- Không thêm dependency mới nếu chưa giải thích lý do.
- Luôn giải thích rủi ro còn lại sau khi sửa.

## Python rules
- Dùng type hints cho code mới.
- Dùng pathlib thay vì path string thủ công.
- Dùng logging thay vì print trong code chạy lâu.
- Không dùng bare except.
- Không nuốt lỗi im lặng.
- Không hard-code COM port, baudrate, đường dẫn output.

## Radar/data rules
- Luôn ghi rõ shape dữ liệu.
- Không nhầm raw ADC data với range FFT/range profile/detected objects.
- Không tự ý đổi thứ tự dimension.
- Với complex data, phải ghi rõ dtype và format I/Q.
- Với buffer lớn, tránh copy không cần thiết.
- Khi parse frame, phải xử lý:
  - magic word lệch
  - frame thiếu byte
  - payload length sai
  - corrupted frame
  - nhiều frame dính liền trong một lần read

## Testing rules
- Nếu sửa parser, phải thêm test parser.
- Nếu sửa xử lý numpy, phải thêm test giữ nguyên shape/output.
- Nếu tối ưu hiệu năng, phải có benchmark hoặc so sánh trước/sau.
- Sau khi sửa, chạy test phù hợp.
- Nếu không chạy được test, nói rõ lý do.

## Workflow
For complex tasks:
1. Read relevant files.
2. Summarize current design.
3. Identify problems.
4. Propose options.
5. Wait for selected option unless user asked to implement directly.
6. Implement smallest safe change.
7. Run tests.
8. Summarize changes and risks.