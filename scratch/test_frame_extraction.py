import cv2
import numpy as np
from pathlib import Path
from src.assets import extract_final_frame

test_dir = Path("scratch/test_video")
test_dir.mkdir(parents=True, exist_ok=True)
video_path = test_dir / "test_segment.mp4"
output_image_path = test_dir / "final_frame.png"

# Generate a 10-frame dummy video
fourcc = cv2.VideoWriter_fourcc(*'mp4v')
out = cv2.VideoWriter(str(video_path), fourcc, 10.0, (640, 360))

for i in range(10):
    img = np.zeros((360, 640, 3), dtype=np.uint8)
    # Write frame index as color gradient
    img[:, :] = (i * 20, 255 - i * 20, 128)
    out.write(img)
out.release()

print(f"Generated test video: {video_path}")

# Test extraction
extracted_path = extract_final_frame(video_path, output_image_path)
assert extracted_path.exists(), "Extracted frame file does not exist!"

# Read extracted frame
extracted_img = cv2.imread(str(extracted_path))
assert extracted_img is not None, "Extracted frame image is unreadable!"
assert extracted_img.shape == (360, 640, 3), f"Unexpected frame shape: {extracted_img.shape}"

print("SUCCESS: extract_final_frame verified working with OpenCV!")
