# Official Playwright image: Ubuntu 24.04, Python 3.12, Chromium and its system libraries.
# The tag must match the playwright version in requirements.txt.
FROM mcr.microsoft.com/playwright/python:v1.65.2-noble

ENV PYTHONUNBUFFERED=1 \
    PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUTF8=1 \
    PIP_NO_CACHE_DIR=1 \
    PIP_DISABLE_PIP_VERSION_CHECK=1 \
    PIP_ROOT_USER_ACTION=ignore

WORKDIR /app
COPY requirements.txt .
RUN pip install -r requirements.txt

COPY airsensor ./airsensor

EXPOSE 8000
CMD ["python", "-m", "airsensor"]
