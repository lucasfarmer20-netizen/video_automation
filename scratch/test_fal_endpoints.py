import os
import fal_client

endpoints_to_test = [
    "fal-ai/kling-video/v3/image-to-video",
    "fal-ai/kling-video/v1.5/pro/image-to-video",
    "fal-ai/bytedance/seedance-2.0/image-to-video",
    "fal-ai/bytedance/seedance-2.0",
    "fal-ai/bytedance/seedance/v1/image-to-video",
    "fal-ai/veo-video/v3/image-to-video",
    "fal-ai/veo/v3/image-to-video",
    "fal-ai/wan-video/v1/image-to-video",
    "fal-ai/hunyuan-video/image-to-video",
    "fal-ai/luma-dream-machine/ray-2/image-to-video",
    "fal-ai/luma-dream-machine",
]

print("Testing Fal.ai endpoints...")
for ep in endpoints_to_test:
    try:
        # Check endpoint metadata/status
        res = fal_client.submit(ep, arguments={"prompt": "test"}, webhook_url="http://localhost")
        print(f"✅ EXISTS: {ep}")
    except Exception as e:
        err_msg = str(e)
        if "not found" in err_msg.lower():
            print(f"❌ NOT FOUND: {ep}")
        elif "missing" in err_msg.lower() or "required" in err_msg.lower() or "validation" in err_msg.lower() or "bad request" in err_msg.lower():
            print(f"✅ EXISTS (Validation Error): {ep}")
        else:
            print(f"❓ RESPONSE ({ep}): {err_msg[:100]}")
