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
    """Turn raw model outputs into [{'label', 'score', 'box'}] (box in model-input pixels for NMS output, else None)."""
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
    failing = [d['score'] for d in detections if not failure_labels or d['label'] in failure_labels]
    return dict(score=max(failing, default=0.0), detections=sorted(detections, key=lambda d: -d['score'])[:10])


def main():
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
                    frame, _, _, _ = letterbox(image, width, height)
                    result = pipeline.infer({info.name: input_batch(frame)})
                    reply = parse(result, class_names, failure_labels)
                except Exception as exc:   # one bad frame never stops the helper
                    reply = {'error': f'{type(exc).__name__}: {exc}'[:300]}
                print(json.dumps(reply), flush=True)


if __name__ == '__main__':
    main()
