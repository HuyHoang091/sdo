import subprocess
import threading
import time
import docker

client = docker.from_env()

def run_slow_client(container_name):
    print(f"[*] Khởi động Slow Client nhắm vào container: {container_name}")
    # Chạy file Python dạng script thẳng bên trong mạng Docker để truy cập bằng tên
    script = f"""
import socket, time
print("1. Client: Đang kết nối tới {container_name}:80...")
try:
    s = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    s.settimeout(20)
    s.connect(('{container_name}', 80))
    print("2. Client: Đã kết nối! Gởi 1 nửa HTTP Request (Cố tình câu giờ)...")
    s.send(b"GET / HTTP/1.1\\r\\nHost: {container_name}\\r\\n")
    
    # Giữ cổng trong 8 giây (Nginx bắt buộc phải đợi mình chạy xong mới được Stop)
    for i in range(8):
        print(f"  -> Client giam kết nối: {{i+1}}/8 giây.")
        time.sleep(1)
        
    print("3. Client: Chốt request (Gửi dấu enter \\r\\n)...")
    s.send(b"\\r\\n")
    
    resp = s.recv(1024)
    if resp:
        print("\\n==================================")
        print("4. HOÀN HẢO! Nhận được HTTP Phản hồi.")
        print(resp.decode('utf-8').splitlines()[0])
        print("=> GRACEFUL DRAIN THÀNH CÔNG: Nginx đã nán lại để đợi bạn thay vì bị mất luôn!")
        print("==================================\\n")
    else:
        print("4. THẤT BẠI: Kết nối bị chặt đứt giữa chừng!")
except Exception as e:
    print("LỖI KẾT NỐI:", e)
"""
    cmd = [
        "docker", "run", "--rm", "--network", "v20_default",
        "python:3.9-alpine", "python", "-c", script
    ]
    subprocess.run(cmd)

def run_new_client(container_name):
    print(f"\\n[+] Khởi động Client LÀ NHẬN VÀO SAU (New Request) nhắm vào: {container_name}")
    script = f"""
import socket
print("-> New Client: Cố gắng mở TCP socket mới tới {container_name}:80...")
try:
    s = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    s.settimeout(3)
    s.connect(('{container_name}', 80))
    print("-> New Client: THẤT BẠI KỊCH BẢN! Cổng vẫn đang mở để nhận khách mới!")
except ConnectionRefusedError:
    print("-> New Client: HOÀN HẢO! Nhận được lỗi Connection Refused. Nginx đã đóng cửa đóng cổng không tiếp khách, ép Load Balancer phải tìm Node khác!")
except Exception as e:
    print(f"-> New Client: HOÀN HẢO! Xin kết nối thất bại ({{e}}). Chặn triệt để request mới!")
"""
    cmd = [
        "docker", "run", "--rm", "--network", "v20_default",
        "python:3.9-alpine", "python", "-c", script
    ]
    subprocess.run(cmd)

if __name__ == "__main__":
    # Tìm 1 API container đang chạy
    api_containers = [c for c in client.containers.list() if c.labels.get("sdo.service") == "api" and c.status == "running"]
    if not api_containers:
        print("Không tìm thấy API container nào đang chạy!")
        exit(1)
        
    target = api_containers[0]
    print(f"-> Đã chọn Mục tiêu Test: {target.name} ({target.short_id})")
    
    # Bắt đầu chạy luồng Client chọc ngang hông
    t = threading.Thread(target=run_slow_client, args=(target.name,))
    t.start()
    
    time.sleep(3) # Cho client connect xong (Chiếm cổng tcp)
    
    print(f"\\n[*] [Agent Giả lập] Bắn tín hiệu SIGQUIT (Draining) vào {target.name}...")
    target.kill(signal="SIGQUIT")
    print("[*] Đã bắn SIGQUIT! Lúc này container từ chối request MỚI, nhưng KHÔNG ĐƯỢC CHẾT, nó phải đợi khách CŨ.")
    
    time.sleep(2) # Cho Nginx kịp xử lý lệnh
    
    # Bắt đầu chạy luồng Client thứ 2 để kiểm chứng cấm cửa
    t2 = threading.Thread(target=run_new_client, args=(target.name,))
    t2.start()
    t2.join()
    
    t.join()
    
    # Kiểm tra trạng thái container sau đó
    try:
        target.reload()
        print(f"-> Trạng thái Container sau khi khách cũ về: {target.status.upper()}")
        if target.status == "exited":
            print("=> THÀNH CÔNG: Nó đã tự sát (EXIT) ngay sau khi khách cuối cùng ra khỏi cửa!")
    except docker.errors.NotFound:
        print("-> Container đã bị Docker dọn dẹp sạch sẽ.")
