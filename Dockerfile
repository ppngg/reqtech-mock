# Mock SAP/Snowflake service for ReqTech local dev.
# Stdlib only — no dependencies to install. Works on Render, Railway, Fly.io, Cloud Run.
FROM python:3.12-slim
WORKDIR /app
COPY mock_sap_snowflake.py .
# Platforms (Render/Railway/Fly/Cloud Run) inject PORT; the app reads it. 8099 is the local default.
ENV PORT=8099
EXPOSE 8099
CMD ["python", "mock_sap_snowflake.py"]
