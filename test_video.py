import cv2

# Test camera
cap = cv2.VideoCapture(0)
if not cap.isOpened():
    print("❌ Camera not accessible")
    exit(1)

ret, frame = cap.read()
if ret and frame is not None:
    print(f"✅ Camera working - Frame shape: {frame.shape}")
    
    # Test face detection
    face_cascade = cv2.CascadeClassifier(cv2.data.haarcascades + "haarcascade_frontalface_default.xml")
    gray = cv2.cvtColor(frame, cv2.COLOR_BGR2GRAY)
    faces = face_cascade.detectMultiScale(gray, 1.1, 5)
    print(f"✅ Face detection: {len(faces)} faces detected")
    
    cap.release()
    print("✅ All systems working!")
else:
    print("❌ Cannot read frame from camera")
    cap.release()