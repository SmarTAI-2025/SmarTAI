import subprocess, time, requests, streamlit as st, atexit, os, signal, sys
import random
import socket

# Function to find a random available port
def get_random_port():
    while True:
        port = random.randint(8000, 9000)  # Choose from range 8000-9000
        with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as s:
            if s.connect_ex(('localhost', port)) != 0:  # Port is available
                return port

def start_backend(backend_port):
    """后端用子进程启动，避免信号冲突"""
    cmd = [sys.executable, "-m", "uvicorn", "backend.main:app",
           "--host", "localhost", "--port", str(backend_port),  # Changed from 127.0.0.1 to localhost
           "--no-access-log"]
    
    # Set backend port as environment variable
    env = os.environ.copy()
    env["BACKEND_PORT"] = str(backend_port)
    
    # Windows compatibility: don't use preexec_fn=os.setsid
    if os.name == 'nt':  # Windows
        return subprocess.Popen(cmd, env=env)
    else:  # Unix/Linux/Mac
        return subprocess.Popen(cmd, env=env, preexec_fn=os.setsid)

def wait_backend_ready(backend_port, timeout=15):
    for _ in range(timeout):
        try:
            r = requests.get(f"http://localhost:{backend_port}/docs", timeout=1)
            if r.status_code == 200:
                return True
        except: pass
        time.sleep(1)
    return False

def cleanup_backend(backend_proc):
    """Clean up the backend process"""
    try:
        if os.name == 'nt':  # Windows
            backend_proc.terminate()
        else:  # Unix/Linux/Mac
            os.killpg(os.getpgid(backend_proc.pid), signal.SIGTERM)
    except Exception as e:
        print(f"Error during cleanup: {e}")

if __name__ == "__main__":
    # 1. 拉起后端
    backend_port = get_random_port()
    backend_proc = start_backend(backend_port)
    atexit.register(lambda: cleanup_backend(backend_proc))

    if not wait_backend_ready(backend_port):
        print("Backend health-check failed")
        sys.exit(1)

    # 2. 启动前端应用
    frontend_path = os.path.join(os.path.dirname(__file__), "frontend/pages", "main.py")
    frontend_port = get_random_port()
    cmd = [sys.executable, "-m", "streamlit", "run", frontend_path, 
           "--server.port", str(frontend_port), "--client.showSidebarNavigation=False"]
    
    # Set backend URL as environment variable
    env = os.environ.copy()
    env["BACKEND_URL"] = f"http://localhost:{backend_port}"
    
    frontend_proc = subprocess.Popen(cmd, env=env)
    
    print(f"Backend started on port {backend_port}")
    print(f"Frontend started on port {frontend_port}")
    print(f"Access the application at: http://localhost:{frontend_port}")
    
    try:
        frontend_proc.wait()
    except KeyboardInterrupt:
        print("Shutting down...")
    finally:
        # Clean up processes
        try:
            frontend_proc.terminate()
            frontend_proc.wait(timeout=5)
        except:
            frontend_proc.kill()
        
        cleanup_backend(backend_proc)