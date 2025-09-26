FROM python:3.9-slim

WORKDIR /app

# Copy requirements and install dependencies
COPY frontend/requirements.txt .
RUN pip install --no-cache-dir -r frontend/requirements.txt

# Copy the application code
COPY . .

# Expose the port Streamlit will run on
EXPOSE 8501

# Set environment variables
ENV STREAMLIT_SERVER_PORT=8501
ENV STREAMLIT_SERVER_ADDRESS=0.0.0.0
ENV STREAMLIT_SERVER_HEADLESS=true

# Run the application
CMD ["streamlit", "run", "frontend/app.py"]