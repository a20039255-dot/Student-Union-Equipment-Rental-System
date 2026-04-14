# 使用 Python 3.9 的輕量版作為基礎
FROM python:3.9-slim

# 設定工作目錄
WORKDIR /app

# 複製套件清單並安裝
COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

# 把所有程式碼複製進貨櫃
COPY . .

# Cloud Run 預設監聽 8080 port
ENV PORT=8080
EXPOSE 8080

# 啟動 FastAPI 的指令
CMD ["uvicorn", "main:app", "--host", "0.0.0.0", "--port", "8080"]