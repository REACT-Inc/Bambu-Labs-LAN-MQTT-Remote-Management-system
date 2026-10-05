"""AI HAT (Hailo) helper for print-failure detection (#70). Runs under the Pi's *system* Python, where Raspberry Pi
OS's `hailo-all` package installs the Hailo library (hailo_platform), numpy and Pillow. The main service starts it and
talks to it over stdin/stdout, one JSON object per line:

    request:  {"jpeg": "<base64 JPEG>"}
    reply:    {"score": 0.83, "detections": [{"label": "spaghetti", "score": 0.83, "box": [x0, y0, x1, y1]}]}
              or {"error": "..."}

The first line it prints is {"ready": true, "input": [width, height]} once the model is loaded on the AI HAT.

Model: a .hef compiled for the HAT's chip (Hailo-8L on the 13 TOPS AI Kit / AI HAT+, Hailo-8 on the 26 TOPS HAT+).
Two output layouts are understood:
- Hailo on-chip NMS (YOLO-style detection HEFs): per class, rows of [ymin, xmin, ymax, xmax, score] (0-1);
- a classification head: one score per class.
Only the classes named in "labels" count as a failure; the score is the highest of those.

A .onnx model (for example best.onnx exported from Ultralytics YOLOv8: `yolo export format=onnx opset=11`) runs on
the Pi's CPU instead, with OpenCV from Raspberry Pi OS (`sudo apt install python3-opencv`); no AI HAT is needed.
One small model every 30 s per printer takes well under a second on a Pi 5.
"""
import base64
import io
import json
import sys


def letterbox(image, width, height):
    """Resize keeping the aspect ratio and pad with grey (YOLO convention); returns (image, scale, pad_x, pad_y)."""
    from PIL import Image
    image = image.convert('RGB')
    scale = min(width / image.width, height / image.height)
    resized = image.resize((max(1, round(image.width * scale)), max(1, round(image.height * scale))))
    canvas = Image.new('RGB', (width, height), (114, 114, 114))
    pad_x, pad_y = (width - resized.width) // 2, (height - resized.height) // 2
    canvas.paste(resized, (pad_x, pad_y))
    return canvas, scale, pad_x, pad_y


def input_batch(frame):
    """A batch of one, as a writeable contiguous uint8 array. np.asarray() on a Pillow image is read-only, which
    HailoRT refuses ("array is not writeable"), so the image is copied with np.array()."""
    import numpy as np
    return np.ascontiguousarray(np.expand_dims(np.array(frame, dtype=np.uint8), 0))


def parse(outputs, class_names, failure_labels, threshold=0.05):
    """Turn raw model outputs into [{'label', 'score', 'box'}] (box as 0-1 fractions of the model input for NMS output, else None)."""
    detections = []
    for value in outputs.values():
        batch = value[0] if isinstance(value, (list, tuple)) and value and isinstance(value[0], (list, tuple)) else value
        if isinstance(batch, (list, tuple)):
            # On-chip NMS: one entry per class, each an array of [ymin, xmin, ymax, xmax, score].
            for index, rows in enumerate(batch):
                label = class_names[index] if index < len(class_names) else f'class{index}'
                for row in (rows.tolist() if hasattr(rows, 'tolist') else rows) or []:
                    if len(row) >= 5 and float(row[4]) >= threshold:
                        detections.append(dict(label=label, score=round(float(row[4]), 4),
                                               box=[float(row[1]), float(row[0]), float(row[3]), float(row[2])]))
        else:
            # Classification head: one score per class.
            scores = value.reshape(-1).tolist() if hasattr(value, 'reshape') else list(value)
            for index, score in enumerate(scores):
                label = class_names[index] if index < len(class_names) else f'class{index}'
                if float(score) >= threshold:
                    detections.append(dict(label=label, score=round(float(score), 4), box=None))
    return summarise(detections, failure_labels)


def to_picture(detections, width, height, scale, pad_x, pad_y, image_width, image_height):
    """Boxes come back as fractions of the letterboxed model input; turn them into fractions of the camera picture
    (so they can be drawn on it and matched against the bed calibration)."""
    for detection in detections:
        box = detection.get('box')
        if box:
            xs = [min(1.0, max(0.0, (box[i] * width - pad_x) / scale / image_width)) for i in (0, 2)]
            ys = [min(1.0, max(0.0, (box[i] * height - pad_y) / scale / image_height)) for i in (1, 3)]
            detection['box'] = [round(xs[0], 4), round(ys[0], 4), round(xs[1], 4), round(ys[1], 4)]
    return detections


def summarise(detections, failure_labels):
    failing = [d['score'] for d in detections if not failure_labels or d['label'] in failure_labels]
    return dict(score=max(failing, default=0.0), detections=sorted(detections, key=lambda d: -d['score'])[:10])


def decode_yolov8(output, class_names, width, height, threshold=0.05, iou=0.45):
    """Ultralytics YOLOv8 ONNX output, (1, 4 + classes, boxes) of centre x, centre y, w, h in input pixels and
    one score per class, into detections with boxes as 0-1 fractions of the input. Overlaps are removed per class."""
    import numpy as np
    rows = np.asarray(output, dtype=np.float32).reshape(np.asarray(output).shape[-2:])
    if rows.shape[0] > rows.shape[1]:
        rows = rows.T   # some exports put boxes first
    boxes, scores = rows[:4].T, rows[4:].T
    classes, best = scores.argmax(axis=1), scores.max(axis=1)
    keep = best >= threshold
    boxes, classes, best = boxes[keep], classes[keep], best[keep]
    corners = np.stack([boxes[:, 0] - boxes[:, 2] / 2, boxes[:, 1] - boxes[:, 3] / 2,
                        boxes[:, 0] + boxes[:, 2] / 2, boxes[:, 1] + boxes[:, 3] / 2], axis=1)
    detections = []
    for index in np.unique(classes):
        chosen = np.where(classes == index)[0]
        chosen = chosen[np.argsort(-best[chosen])]
        while chosen.size and len(detections) < 100:
            top, chosen = chosen[0], chosen[1:]
            box = corners[top]
            label = class_names[index] if index < len(class_names) else f'class{index}'
            detections.append(dict(label=label, score=round(float(best[top]), 4),
                                   box=[round(float(box[0] / width), 4), round(float(box[1] / height), 4),
                                        round(float(box[2] / width), 4), round(float(box[3] / height), 4)]))
            if chosen.size:
                other = corners[chosen]
                x0, y0 = np.maximum(box[0], other[:, 0]), np.maximum(box[1], other[:, 1])
                x1, y1 = np.minimum(box[2], other[:, 2]), np.minimum(box[3], other[:, 3])
                overlap = np.clip(x1 - x0, 0, None) * np.clip(y1 - y0, 0, None)
                area = lambda b: (b[..., 2] - b[..., 0]) * (b[..., 3] - b[..., 1])
                chosen = chosen[overlap / (area(box) + area(other) - overlap + 1e-9) < iou]
    return detections


def main_cpu(model, class_names, failure_labels, size):
    """Run a YOLOv8 .onnx model on the CPU with OpenCV."""
    import cv2
    import numpy as np
    from PIL import Image
    cv2.setNumThreads(2)   # leave cores free for the dashboard, MQTT and the cameras
    net = cv2.dnn.readNetFromONNX(model)
    print(json.dumps({'ready': True, 'input': [size, size], 'backend': 'cpu'}), flush=True)
    for line in sys.stdin:
        try:
            request = json.loads(line)
            image = Image.open(io.BytesIO(base64.b64decode(request['jpeg'])))
            frame, scale, pad_x, pad_y = letterbox(image, size, size)
            # The letterboxed frame is already RGB, as Ultralytics models expect.
            net.setInput(cv2.dnn.blobFromImage(np.array(frame), 1 / 255.0, (size, size), swapRB=False, crop=False))
            detections = to_picture(decode_yolov8(net.forward(), class_names, size, size), size, size, scale, pad_x, pad_y, image.width, image.height)
            reply = dict(summarise(detections, failure_labels), image=[image.width, image.height])
        except Exception as exc:   # one bad frame never stops the helper
            reply = {'error': f'{type(exc).__name__}: {exc}'[:300]}
        print(json.dumps(reply), flush=True)


def main():
    if sys.argv[1].lower().endswith('.onnx'):
        return main_cpu(sys.argv[1], json.loads(sys.argv[2]), set(json.loads(sys.argv[3])),
                        int(sys.argv[4]) if len(sys.argv) > 4 else 640)
    import numpy as np
    from PIL import Image
    from hailo_platform import (HEF, VDevice, ConfigureParams, HailoStreamInterface, InferVStreams,
                                InputVStreamParams, OutputVStreamParams, FormatType)
    model, class_names, failure_labels = sys.argv[1], json.loads(sys.argv[2]), set(json.loads(sys.argv[3]))
    hef = HEF(model)
    with VDevice() as device:
        group = device.configure(hef, ConfigureParams.create_from_hef(hef, interface=HailoStreamInterface.PCIe))[0]
        info = hef.get_input_vstream_infos()[0]
        height, width = info.shape[0], info.shape[1]
        inputs = InputVStreamParams.make(group, format_type=FormatType.UINT8)
        outputs = OutputVStreamParams.make(group, format_type=FormatType.FLOAT32)
        with InferVStreams(group, inputs, outputs) as pipeline, group.activate(group.create_params()):
            print(json.dumps({'ready': True, 'input': [width, height]}), flush=True)
            for line in sys.stdin:
                try:
                    request = json.loads(line)
                    image = Image.open(io.BytesIO(base64.b64decode(request['jpeg'])))
                    frame, scale, pad_x, pad_y = letterbox(image, width, height)
                    result = pipeline.infer({info.name: input_batch(frame)})
                    reply = parse(result, class_names, failure_labels)
                    to_picture(reply['detections'], width, height, scale, pad_x, pad_y, image.width, image.height)
                    reply['image'] = [image.width, image.height]
                except Exception as exc:   # one bad frame never stops the helper
                    reply = {'error': f'{type(exc).__name__}: {exc}'[:300]}
                print(json.dumps(reply), flush=True)


if __name__ == '__main__':
    main()
