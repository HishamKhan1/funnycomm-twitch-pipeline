# Models

Model files aren't stored in git. Put them here:

| File | Used for | Source |
|---|---|---|
| `face_detection_yunet_2023mar.onnx` | Face detection and framing (`funnycomm/vision.py`) | [OpenCV Zoo: YuNet](https://github.com/opencv/opencv_zoo/tree/main/models/face_detection_yunet) |
| `audio/ced_mini.int8.onnx` | Laughter, scream, crowd and music tagging (`funnycomm/ears.py`) | [sherpa-onnx audio-tagging models](https://github.com/k2-fsa/sherpa-onnx/releases/tag/audio-tagging-models): the CED mini archive's `model.int8.onnx`, renamed |
| `audio/class_labels_indices.csv` | AudioSet label names for the tagger | Same archive as above |

These are downloaded for you by the install scripts:

- `engine/models/selfie_segmenter_landscape.tflite`: MediaPipe person segmentation (`Install_Optional_AI.bat`)
- The faster-whisper speech model (`Install_Smart_Editing.bat`)

The app still runs without the optional models. Their features just switch off, and the Smart Editing installer shows what's ready.
