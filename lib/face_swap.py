"""
Face Swap module for LUMN Studio.
Uses OpenCV for face detection and seamless clone blending.
Works with Python 3.14 — no insightface dependency needed.

Optional ONNX-based face swap can be enabled when models are available:
- det_10g.onnx (face detection, ~16MB)
- inswapper_128.onnx (face swap, ~555MB)

Models are downloaded on first use to: output/models/
"""

import os
import subprocess
import sys

import cv2
import numpy as np

try:
    import onnxruntime as ort
    HAS_ORT = True
except ImportError:
    HAS_ORT = False

MODELS_DIR = None
_ONNX_MODE = False  # Set True when ONNX models are available and working

# Lazy-loaded ONNX sessions
_det_session = None
_swap_session = None

_DET_MODEL_URL = "https://huggingface.co/datasets/Gourieff/ReActor/resolve/main/models/det_10g.onnx"
_SWAP_MODEL_URL = "https://huggingface.co/datasets/Gourieff/ReActor/resolve/main/models/inswapper_128.onnx"


def _subprocess_kwargs() -> dict:
    """Extra kwargs for subprocess calls (hide window on Windows)."""
    kw: dict = {}
    if sys.platform == "win32":
        si = subprocess.STARTUPINFO()
        si.dwFlags |= subprocess.STARTF_USESHOWWINDOW
        si.wShowWindow = 0
        kw["startupinfo"] = si
        kw["creationflags"] = 0x08000000  # CREATE_NO_WINDOW
    return kw


def init(output_dir: str, onnx_mode: bool = False):
    """Initialize the face swap module.

    Args:
        output_dir: base output directory (models stored in output_dir/models/)
        onnx_mode: if True, attempt to download and use ONNX models for
                   higher-quality face swap. Falls back to OpenCV if unavailable.
    """
    global MODELS_DIR, _ONNX_MODE
    MODELS_DIR = os.path.join(output_dir, "models")
    os.makedirs(MODELS_DIR, exist_ok=True)
    _ONNX_MODE = onnx_mode and HAS_ORT


# ---------------------------------------------------------------------------
# Model download helpers
# ---------------------------------------------------------------------------

def _ensure_models():
    """Download ONNX models if not present. Returns (det_path, swap_path)."""
    if MODELS_DIR is None:
        raise RuntimeError("face_swap.init() must be called first")

    det_path = os.path.join(MODELS_DIR, "det_10g.onnx")
    swap_path = os.path.join(MODELS_DIR, "inswapper_128.onnx")

    if not os.path.isfile(det_path):
        print("[FaceSwap] Downloading face detection model...")
        import urllib.request
        urllib.request.urlretrieve(_DET_MODEL_URL, det_path)
        print(f"[FaceSwap] Saved: {det_path}")

    if not os.path.isfile(swap_path):
        print("[FaceSwap] Downloading face swap model (555MB)...")
        import urllib.request
        urllib.request.urlretrieve(_SWAP_MODEL_URL, swap_path)
        print(f"[FaceSwap] Saved: {swap_path}")

    return det_path, swap_path


def _get_det_session():
    """Get (or create) the ONNX detection session."""
    global _det_session
    if _det_session is None:
        det_path, _ = _ensure_models()
        _det_session = ort.InferenceSession(det_path, providers=["CPUExecutionProvider"])
    return _det_session


def _get_swap_session():
    """Get (or create) the ONNX swap session."""
    global _swap_session
    if _swap_session is None:
        _, swap_path = _ensure_models()
        _swap_session = ort.InferenceSession(swap_path, providers=["CPUExecutionProvider"])
    return _swap_session


# ---------------------------------------------------------------------------
# OpenCV face detection (always available, no extra models needed)
# ---------------------------------------------------------------------------

_haar_cascade = None


def _get_haar_cascade():
    global _haar_cascade
    if _haar_cascade is None:
        cascade_path = cv2.data.haarcascades + "haarcascade_frontalface_default.xml"
        _haar_cascade = cv2.CascadeClassifier(cascade_path)
    return _haar_cascade


def detect_faces_opencv(img):
    """Detect faces using OpenCV Haar cascade (always available).

    Returns list of dicts with 'bbox' [x1, y1, x2, y2] and 'score'.
    """
    cascade = _get_haar_cascade()
    gray = cv2.cvtColor(img, cv2.COLOR_BGR2GRAY)
    faces = cascade.detectMultiScale(
        gray, scaleFactor=1.1, minNeighbors=5, minSize=(30, 30)
    )

    result = []
    for (x, y, w, h) in faces:
        result.append({
            "bbox": [int(x), int(y), int(x + w), int(y + h)],
            "score": 1.0,
        })
    return result


# ---------------------------------------------------------------------------
# ONNX face detection (higher quality, requires model download)
# ---------------------------------------------------------------------------

def detect_faces_onnx(img):
    """Detect faces using the det_10g ONNX model.

    The det_10g.onnx model from buffalo_l uses a multi-stride anchor-free
    detector.  The outputs vary by stride (8, 16, 32) and contain scores,
    bounding-box deltas, and landmark deltas.  We parse them by output shape.

    Returns list of dicts with 'bbox' [x1, y1, x2, y2] and 'score'.
    """
    session = _get_det_session()

    h, w = img.shape[:2]
    target_size = 640
    scale = target_size / max(h, w)
    nh, nw = int(h * scale), int(w * scale)
    resized = cv2.resize(img, (nw, nh))

    # Pad to target_size x target_size
    padded = np.zeros((target_size, target_size, 3), dtype=np.float32)
    padded[:nh, :nw] = resized.astype(np.float32)

    # Normalize (standard ImageNet-style)
    input_tensor = (padded - 127.5) / 128.0
    input_tensor = input_tensor.transpose(2, 0, 1)[np.newaxis].astype(np.float32)

    input_name = session.get_inputs()[0].name
    outputs = session.run(None, {input_name: input_tensor})

    # Parse multi-stride outputs
    # det_10g outputs 9 tensors: for each stride (8,16,32) -> (scores, bbox, kps)
    # scores shape: (1, num_anchors, 1)
    # bbox shape:   (1, num_anchors, 4)
    # kps shape:    (1, num_anchors, 10)
    faces = []
    strides = [8, 16, 32]
    feat_stride_idx = 0

    i = 0
    while i < len(outputs):
        # Try to identify groups of 3: scores, bboxes, landmarks
        out = outputs[i]

        # Scores tensor: last dim == 1
        if out.ndim >= 2 and out.shape[-1] == 1:
            scores = out.reshape(-1)
            # Next should be bboxes (last dim == 4)
            if i + 1 < len(outputs) and outputs[i + 1].shape[-1] == 4:
                bboxes = outputs[i + 1].reshape(-1, 4)

                stride = strides[feat_stride_idx] if feat_stride_idx < len(strides) else 8
                fh = target_size // stride
                fw = target_size // stride

                for idx in range(len(scores)):
                    if scores[idx] > 0.5:
                        # Anchor center
                        ay = (idx // fw) * stride
                        ax = (idx % fw) * stride

                        # Decode bbox (center offsets + size)
                        dx, dy, dw_val, dh_val = bboxes[idx]
                        cx = ax + dx * stride
                        cy = ay + dy * stride
                        bw = np.exp(dw_val) * stride
                        bh = np.exp(dh_val) * stride

                        x1 = (cx - bw / 2) / scale
                        y1 = (cy - bh / 2) / scale
                        x2 = (cx + bw / 2) / scale
                        y2 = (cy + bh / 2) / scale

                        faces.append({
                            "bbox": [
                                max(0, int(x1)),
                                max(0, int(y1)),
                                min(w, int(x2)),
                                min(h, int(y2)),
                            ],
                            "score": float(scores[idx]),
                        })

                feat_stride_idx += 1
                # Skip bbox (and optional kps)
                if i + 2 < len(outputs) and outputs[i + 2].shape[-1] == 10:
                    i += 3  # scores + bbox + kps
                else:
                    i += 2  # scores + bbox
                continue

        i += 1

    # NMS
    if faces:
        faces = _nms(faces, iou_threshold=0.4)

    return faces


def _nms(faces, iou_threshold=0.4):
    """Simple non-maximum suppression."""
    faces = sorted(faces, key=lambda f: f["score"], reverse=True)
    keep = []
    for face in faces:
        discard = False
        for kept in keep:
            if _iou(face["bbox"], kept["bbox"]) > iou_threshold:
                discard = True
                break
        if not discard:
            keep.append(face)
    return keep


def _iou(a, b):
    """Intersection over union for two bboxes [x1,y1,x2,y2]."""
    x1 = max(a[0], b[0])
    y1 = max(a[1], b[1])
    x2 = min(a[2], b[2])
    y2 = min(a[3], b[3])
    inter = max(0, x2 - x1) * max(0, y2 - y1)
    area_a = (a[2] - a[0]) * (a[3] - a[1])
    area_b = (b[2] - b[0]) * (b[3] - b[1])
    union = area_a + area_b - inter
    return inter / union if union > 0 else 0


# ---------------------------------------------------------------------------
# Unified detect_faces — picks best available backend
# ---------------------------------------------------------------------------

def detect_faces(img):
    """Detect faces using the best available backend.

    Returns list of dicts with 'bbox' [x1, y1, x2, y2] and 'score'.
    """
    if _ONNX_MODE:
        try:
            return detect_faces_onnx(img)
        except Exception as e:
            print(f"[FaceSwap] ONNX detection failed, falling back to OpenCV: {e}")
    return detect_faces_opencv(img)


# ---------------------------------------------------------------------------
# Face swap — OpenCV seamless clone (always works)
# ---------------------------------------------------------------------------

def swap_face_opencv(frame, source_face_img, target_bbox):
    """Swap a face into frame using OpenCV seamless clone.

    Args:
        frame: the target frame (BGR numpy array)
        source_face_img: cropped source face image (BGR numpy array)
        target_bbox: [x1, y1, x2, y2] of the target face in frame

    Returns:
        frame with the face swapped in
    """
    x1, y1, x2, y2 = [int(v) for v in target_bbox]
    h, w = frame.shape[:2]
    x1, y1 = max(0, x1), max(0, y1)
    x2, y2 = min(w, x2), min(h, y2)

    face_w, face_h = x2 - x1, y2 - y1
    if face_w < 10 or face_h < 10:
        return frame

    # Resize source face to target size
    resized_source = cv2.resize(source_face_img, (face_w, face_h))

    # Create elliptical mask for natural blending
    mask = np.zeros((face_h, face_w), dtype=np.uint8)
    pad_x = max(2, face_w // 10)
    pad_y = max(2, face_h // 10)
    cv2.ellipse(
        mask,
        (face_w // 2, face_h // 2),
        (face_w // 2 - pad_x, face_h // 2 - pad_y),
        0, 0, 360, 255, -1,
    )
    mask = cv2.GaussianBlur(mask, (15, 15), 10)

    # Center point for seamless clone
    center = (x1 + face_w // 2, y1 + face_h // 2)

    # Ensure center is within frame bounds
    center = (
        max(face_w // 2 + 1, min(w - face_w // 2 - 1, center[0])),
        max(face_h // 2 + 1, min(h - face_h // 2 - 1, center[1])),
    )

    try:
        output = cv2.seamlessClone(resized_source, frame, mask, center, cv2.NORMAL_CLONE)
        return output
    except cv2.error as e:
        print(f"[FaceSwap] seamlessClone failed: {e}")
        return frame


# ---------------------------------------------------------------------------
# Face swap — ONNX inswapper (higher quality, requires model)
# ---------------------------------------------------------------------------

def swap_face_onnx(frame, source_face_img, target_bbox):
    """Swap a face using inswapper_128 ONNX model.

    The inswapper model expects:
      - Input 0: target face aligned to 128x128 (NCHW, float32, /255)
      - Input 1: source latent (from recognition model or simplified embedding)

    Since we don't have the full insightface recognition model, we use
    the source face crop resized to the expected input shape and rely on
    the model to produce a plausible blend.

    Falls back to OpenCV seamless clone if anything goes wrong.
    """
    try:
        session = _get_swap_session()

        x1, y1, x2, y2 = [int(v) for v in target_bbox]
        h, w = frame.shape[:2]
        x1, y1 = max(0, x1), max(0, y1)
        x2, y2 = min(w, x2), min(h, y2)

        target_face = frame[y1:y2, x1:x2]
        if target_face.size == 0:
            return frame

        # Prepare inputs based on model's expected shapes
        inputs = session.get_inputs()
        input_shapes = {inp.name: inp.shape for inp in inputs}

        # Target face — typically 128x128
        target_h = inputs[0].shape[2] if len(inputs[0].shape) == 4 else 128
        target_w = inputs[0].shape[3] if len(inputs[0].shape) == 4 else 128
        target_input = cv2.resize(target_face, (target_w, target_h)).astype(np.float32)
        target_input = target_input.transpose(2, 0, 1)[np.newaxis] / 255.0

        # Source face — model may expect latent or image
        if len(inputs) >= 2:
            src_shape = inputs[1].shape
            if len(src_shape) == 4:
                # Image input
                src_h = src_shape[2] if src_shape[2] is not None else 128
                src_w = src_shape[3] if src_shape[3] is not None else 128
                source_input = cv2.resize(source_face_img, (src_w, src_h)).astype(np.float32)
                source_input = source_input.transpose(2, 0, 1)[np.newaxis] / 255.0
            elif len(src_shape) == 2:
                # Latent vector — create a simple embedding from the face
                latent_dim = src_shape[1] if src_shape[1] is not None else 512
                face_112 = cv2.resize(source_face_img, (112, 112)).astype(np.float32)
                face_flat = ((face_112 - 127.5) / 127.5).flatten()
                # Project to expected dimension
                if len(face_flat) > latent_dim:
                    source_input = face_flat[:latent_dim].reshape(1, latent_dim)
                else:
                    source_input = np.zeros((1, latent_dim), dtype=np.float32)
                    source_input[0, :len(face_flat)] = face_flat
            else:
                raise ValueError(f"Unexpected source input shape: {src_shape}")
        else:
            raise ValueError("Model has fewer than 2 inputs")

        # Run swap model
        input_names = [inp.name for inp in inputs]
        feed = {input_names[0]: target_input}
        if len(input_names) >= 2:
            feed[input_names[1]] = source_input

        result = session.run(None, feed)

        # Post-process swapped face
        swapped = result[0][0].transpose(1, 2, 0)
        if swapped.max() <= 1.0:
            swapped = swapped * 255
        swapped = np.clip(swapped, 0, 255).astype(np.uint8)
        swapped_resized = cv2.resize(swapped, (x2 - x1, y2 - y1))

        # Blend back with seamless clone for smoother edges
        face_h, face_w = y2 - y1, x2 - x1
        mask = np.zeros((face_h, face_w), dtype=np.uint8)
        pad_x = max(2, face_w // 8)
        pad_y = max(2, face_h // 8)
        cv2.ellipse(
            mask,
            (face_w // 2, face_h // 2),
            (face_w // 2 - pad_x, face_h // 2 - pad_y),
            0, 0, 360, 255, -1,
        )
        mask = cv2.GaussianBlur(mask, (11, 11), 8)

        center = (x1 + face_w // 2, y1 + face_h // 2)
        center = (
            max(face_w // 2 + 1, min(w - face_w // 2 - 1, center[0])),
            max(face_h // 2 + 1, min(h - face_h // 2 - 1, center[1])),
        )

        try:
            output = cv2.seamlessClone(swapped_resized, frame, mask, center, cv2.NORMAL_CLONE)
            return output
        except cv2.error:
            output = frame.copy()
            output[y1:y2, x1:x2] = swapped_resized
            return output

    except Exception as e:
        print(f"[FaceSwap] ONNX swap failed, falling back to OpenCV: {e}")
        return swap_face_opencv(frame, source_face_img, target_bbox)


# ---------------------------------------------------------------------------
# Unified swap_face_in_frame
# ---------------------------------------------------------------------------

def swap_face_in_frame(frame, source_face_img, target_bbox):
    """Swap a face in a single frame using the best available method."""
    if _ONNX_MODE:
        return swap_face_onnx(frame, source_face_img, target_bbox)
    return swap_face_opencv(frame, source_face_img, target_bbox)


# ---------------------------------------------------------------------------
# Full video face swap pipeline
# ---------------------------------------------------------------------------

def swap_faces_in_video(video_path, source_face_path, output_path, progress_cb=None):
    """
    Swap faces in an entire video file.

    Args:
        video_path: path to input video
        source_face_path: path to the source face photo (the face we want)
        output_path: path to save the output video
        progress_cb: optional callback(percent) for progress updates

    Returns:
        output_path on success, None on failure
    """
    if MODELS_DIR is None:
        print("[FaceSwap] Module not initialized — call face_swap.init() first")
        return None

    # Read source face
    source_img = cv2.imread(source_face_path)
    if source_img is None:
        print(f"[FaceSwap] Could not read source face: {source_face_path}")
        return None

    # Detect face in source image
    source_faces = detect_faces(source_img)
    if not source_faces:
        print("[FaceSwap] No face detected in source image")
        return None

    # Crop the source face with some padding
    sf = source_faces[0]
    sx1, sy1, sx2, sy2 = sf["bbox"]
    sh, sw = source_img.shape[:2]
    # Add 10% padding
    pad_x = int((sx2 - sx1) * 0.1)
    pad_y = int((sy2 - sy1) * 0.1)
    sx1 = max(0, sx1 - pad_x)
    sy1 = max(0, sy1 - pad_y)
    sx2 = min(sw, sx2 + pad_x)
    sy2 = min(sh, sy2 + pad_y)
    source_face_crop = source_img[sy1:sy2, sx1:sx2]

    if source_face_crop.size == 0:
        print("[FaceSwap] Source face crop is empty")
        return None

    # Open video
    cap = cv2.VideoCapture(video_path)
    if not cap.isOpened():
        print(f"[FaceSwap] Could not open video: {video_path}")
        return None

    fps = cap.get(cv2.CAP_PROP_FPS) or 24
    width = int(cap.get(cv2.CAP_PROP_FRAME_WIDTH))
    height = int(cap.get(cv2.CAP_PROP_FRAME_HEIGHT))
    total_frames = int(cap.get(cv2.CAP_PROP_FRAME_COUNT))

    # Write to temp file, then add audio back
    temp_output = output_path + ".temp.mp4"
    fourcc = cv2.VideoWriter_fourcc(*"mp4v")
    writer = cv2.VideoWriter(temp_output, fourcc, fps, (width, height))

    frame_idx = 0
    swap_count = 0
    while True:
        ret, frame = cap.read()
        if not ret:
            break

        # Detect faces in this frame
        faces = detect_faces(frame)

        if faces:
            # Swap the largest/most prominent face
            best_face = max(
                faces,
                key=lambda f: (f["bbox"][2] - f["bbox"][0]) * (f["bbox"][3] - f["bbox"][1]),
            )
            frame = swap_face_in_frame(frame, source_face_crop, best_face["bbox"])
            swap_count += 1

        writer.write(frame)
        frame_idx += 1

        if progress_cb and total_frames > 0:
            pct = int((frame_idx / total_frames) * 100)
            progress_cb(pct)

    cap.release()
    writer.release()

    if frame_idx == 0:
        print("[FaceSwap] No frames processed")
        if os.path.isfile(temp_output):
            os.unlink(temp_output)
        return None

    print(f"[FaceSwap] Processed {frame_idx} frames, swapped faces in {swap_count}")

    # Re-mux with audio from original using ffmpeg
    try:
        subprocess.run(
            [
                "ffmpeg", "-y",
                "-i", temp_output,
                "-i", video_path,
                "-c:v", "libx264", "-preset", "fast", "-crf", "18",
                "-c:a", "copy", "-map", "0:v", "-map", "1:a?",
                "-shortest", output_path,
            ],
            capture_output=True,
            timeout=120,
            **_subprocess_kwargs(),
        )
        if os.path.isfile(temp_output):
            os.unlink(temp_output)
    except Exception as e:
        print(f"[FaceSwap] ffmpeg remux failed: {e}")
        # Fall back to the temp file without audio
        if os.path.isfile(temp_output):
            if os.path.isfile(output_path):
                os.unlink(output_path)
            os.rename(temp_output, output_path)

    if os.path.isfile(output_path):
        print(f"[FaceSwap] Output saved: {output_path} ({frame_idx} frames)")
        return output_path

    return None
