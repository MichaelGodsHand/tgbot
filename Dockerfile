# Use the official Python image
FROM python:3.11

# Set the working directory inside the container
WORKDIR /app

# Copy requirements.txt first for caching
COPY requirements.txt .

# Install dependencies
RUN pip install --no-cache-dir -r requirements.txt

# Copy the rest of the app files
COPY . .

# Expose port 8080
EXPOSE 8080

# Run the FastAPI app using uvicorn
CMD ["python", "agent.py"]
