"""Depth module: depth estimation and parallax layer separation.

Depth map -> foreground/midground/background slices -> gap inpaint, so the 2.5D
camera moves in motion.py never reveal holes. Local and free (ONNX Runtime
DirectML or CPU; OpenCV inpaint). No CUDA assumption.

TODO: implement.
"""
