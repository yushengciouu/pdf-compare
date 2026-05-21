FROM python:3.11-slim

WORKDIR /app

ENV PYTHONDONTWRITEBYTECODE=1
ENV PYTHONUNBUFFERED=1

# 安裝系統依賴（poppler 供 PDF 渲染用）
RUN apt-get update && apt-get install -y --no-install-recommends \
    poppler-utils \
    && rm -rf /var/lib/apt/lists/*

# 先複製 requirements 以利用 Docker layer cache
COPY requirements.txt ./
RUN pip install --no-cache-dir -r requirements.txt

# 複製應用程式碼
COPY app ./app

# 建立非 root 使用者以提升安全性
RUN addgroup --system appgroup && adduser --system --ingroup appgroup appuser \
    && mkdir -p /var/compare/jobs \
    && chown -R appuser:appgroup /app /var/compare

USER appuser

CMD ["uvicorn", "app.main:app", "--host", "0.0.0.0", "--port", "8000"]
