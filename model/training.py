import argparse
import json
import os
import typing as ty

import tensorflow as tf
from tensorflow import keras
import keras_cv
from keras_cv import bounding_box
from keras import Model
from .combined_nms import CombinedNMS

TFLITE_OPS = [
    tf.lite.OpsSet.TFLITE_BUILTINS,  # enable TensorFlow Lite ops.
    tf.lite.OpsSet.SELECT_TF_OPS,  # enable TensorFlow ops.
]

TFLITE_OPTIMIZATIONS = [tf.lite.Optimize.DEFAULT]

labels_filename = "labels.txt"


def parse_args():
    """Returns dataset file, model output directory, and num_epochs if present. These must be parsed as command line
    arguments and then used as the model input and output, respectively. The number of epochs can be used to optionally override the default.
    """
    parser = argparse.ArgumentParser()
    parser.add_argument("--dataset_file", dest="data_json", type=str)
    parser.add_argument("--model_output_directory", dest="model_dir", type=str)
    parser.add_argument("--num_epochs", dest="num_epochs", type=int)
    args = parser.parse_args()
    return args.data_json, args.model_dir, args.num_epochs


def parse_filenames_and_bboxes_from_json(
    filename: str,
    all_labels: ty.List[str],
) -> ty.Tuple[ty.List[str], ty.List[str], ty.List[ty.List[float]]]:
    """Load and parse JSON file to return image filenames and corresponding labels with bboxes.
        The JSON file contains lines, where each line has the key "image_path" and "bounding_box_annotations".
    Args:
        filename: JSONLines file containing filenames and bboxes
        all_labels: list of all N_LABELS
    """
    image_filenames = []
    bbox_labels = []
    bbox_coords = []

    with open(filename, "rb") as f:
        for line in f:
            json_line = json.loads(line)
            image_filenames.append(json_line["image_path"])
            annotations = json_line["bounding_box_annotations"]
            labels = []
            coords = []
            for annotation in annotations:
                if annotation["annotation_label"] in all_labels:
                    labels.append(annotation["annotation_label"])
                    # Store coordinates in rel_yxyx format so that we can use the keras_cv function
                    coords.append(
                        [
                            annotation["y_min_normalized"],
                            annotation["x_min_normalized"],
                            annotation["y_max_normalized"],
                            annotation["x_max_normalized"],
                        ]
                    )
            bbox_labels.append(labels)
            bbox_coords.append(coords)
    return image_filenames, bbox_labels, bbox_coords


def parse_image_and_encode_bboxes(
    data: tf.data.Dataset,
    all_labels: ty.List[str],
    src_bbox_format: str,
    tgt_bbox_format: str,
    img_size: ty.Tuple[int, int] = (256, 256),
) -> dict:
    """Returns a dictionary of normalized image array, integer encoded labels array, and bounding box coordinates.
    Args:
        data: dataset in dictionary format containing images and their bounding boxes
        all_labels: list of all N_LABELS
        src_bbox_format: input format of the bboxes
        tgt_bbox_format: format of the bboxes for use in model training
    """
    image_string = tf.io.read_file(data["images"])
    image_decoded = tf.image.decode_image(
        image_string,
        channels=3,
        expand_animations=False,
        dtype=tf.dtypes.uint8,
    )
    # Resize it to fixed shape
    image_resized = tf.image.resize(image_decoded, [img_size[0], img_size[1]])
    # Convert string labels to encoded labels
    encoder = tf.keras.layers.StringLookup(
        vocabulary=all_labels, num_oov_indices=0, output_mode="int"
    )
    labels_encoded = encoder(data["bounding_boxes"]["classes"])
    # Convert bboxes to their intended format
    boxes = convert_bboxes(
        data["bounding_boxes"]["boxes"], src_bbox_format, tgt_bbox_format, image_resized
    )
    data["images"] = image_resized
    data["bounding_boxes"]["classes"] = labels_encoded
    data["bounding_boxes"]["boxes"] = boxes
    return data


def convert_bboxes(
    bboxes: tf.Tensor,
    src_bbox_format: str,
    tgt_bbox_format: str,
    image: tf.Tensor = None,
    image_shape: ty.List[int] = None,
):
    """Converts bounding_boxes from one format to another using keras_cv function
    Args:
        bboxes: tf.Tensor representing bounding boxes in the format specified in the
        src_bbox_format: input format of the bboxes
        tgt_bbox_format: format of the bboxes for use in model training
        image: a batch of images aligned with `boxes` on the first axis
        image_shape: [h, w, channels]
    """
    return keras_cv.bounding_box.convert_format(
        bboxes,
        images=image,
        image_shape=image_shape,
        source=src_bbox_format,
        target=tgt_bbox_format,
    )


def convert_to_tuple(inputs: dict, max_boxes: int) -> ty.Tuple[tf.Tensor, tf.Tensor]:
    """Converts dictionary of inputs into tuple of images and their corresponding bounding boxes
    Args:
        inputs: nested dictionary of data with keys "images" and "bounding_boxes", where "bounding_boxes" contains "classes" and "boxes"
        max_boxes: maximum number of bounding boxes per image
    """
    return inputs["images"], bounding_box.to_dense(
        inputs["bounding_boxes"], max_boxes=max_boxes
    )


def create_dataset_detection(
    filenames: ty.List[str],
    classes: ty.List[str],
    boxes: ty.List[int],
    all_labels: ty.List[str],
    src_bbox_format: str,
    tgt_bbox_format: str,
    target_shape: ty.Tuple[int, int] = (256, 256, 3),
    max_boxes: int = 32,
    train_split: float = 0.8,
    batch_size: int = 64,
    shuffle_buffer_size: int = 1024,
    num_parallel_calls: int = tf.data.experimental.AUTOTUNE,
    prefetch_buffer_size: int = tf.data.experimental.AUTOTUNE,
) -> ty.Tuple[tf.data.Dataset, tf.data.Dataset, tf.data.Dataset]:
    """Load and parse dataset into Tensorflow datasets.
    Args:
        filenames: string list of image paths
        classes: list of string lists, where each string list contains up to max_boxes labels associated with bboxes
        boxes: list of nested int lists, where each int list contains up to max_boxes of the four coordinates identifying bboxes
        all_labels: string list of all N_LABELS
        src_bbox_format: input format of the bboxes
        tgt_bbox_format: format of the bboxes for use in model training
        target_shape: optional 3D shape of image
        max_boxes: maximum number of bounding boxes per image
        train_split: optional float between 0.0 and 1.0 to specify proportion of images that will be used for training
        batch_size: optional size for number of samples for each training iteration
        shuffle_buffer_size: optional size for buffer that will be filled and randomly sampled from, with replacement
        num_parallel_calls: optional integer representing the number of batches to compute asynchronously in parallel
        prefetch_buffer_size: optional integer representing the number of batches that will be buffered when prefetching

    """
    # Create a first dataset of file paths and labels
    dataset = tf.data.Dataset.from_tensor_slices(
        {
            "images": filenames,
            "bounding_boxes": {
                # Turn boxes and classes into ragged tensors as the inputs may be non-rectangular
                # This happens when we have a different number of bboxes per image
                "boxes": tf.ragged.constant(boxes, ragged_rank=1),
                "classes": tf.ragged.constant(classes),
            },
        }
    )

    # Apply a map to the dataset that converts filenames, text labels, and bounding boxes
    # to normalized images, encoded labels, and bounding boxes coordinates, respectively.
    def mapping_fnc(x):
        return parse_image_and_encode_bboxes(
            x, all_labels, src_bbox_format, tgt_bbox_format, target_shape[0:2]
        )

    # Parse and preprocess observations in parallel
    dataset = dataset.map(mapping_fnc, num_parallel_calls=num_parallel_calls)

    # Shuffle the data for each buffer size
    # Disabling reshuffling ensures items from the training and test set will not get shuffled into each other
    dataset = dataset.shuffle(
        buffer_size=shuffle_buffer_size, reshuffle_each_iteration=False
    )

    train_size = int(train_split * len(filenames))
    val_size = int((1 - train_split) * 0.5 * len(filenames))
    test_size = len(filenames) - train_size - val_size
    train_dataset = dataset.take(train_size)
    val_dataset = dataset.skip(train_size).take(val_size)
    test_dataset = dataset.skip(val_size + train_size)

    # Batch the data for multiple steps
    # If the size of training, validation, or testing data is smaller than the batch size,
    # batch the data to expand the dimensions by a length 1 axis.
    # This will ensure that the training data is valid model input
    train_batch_size = batch_size if batch_size < train_size else train_size
    train_dataset = train_dataset.apply(
        tf.data.experimental.dense_to_ragged_batch(train_batch_size)
    )
    val_batch_size = batch_size if batch_size < val_size else val_size
    val_dataset = val_dataset.apply(
        tf.data.experimental.dense_to_ragged_batch(val_batch_size)
    )
    test_batch_size = batch_size if batch_size < test_size else test_size
    test_dataset = test_dataset.apply(
        tf.data.experimental.dense_to_ragged_batch(test_batch_size)
    )
    inference_resizing = keras_cv.layers.Resizing(
        target_shape[0],
        target_shape[1],
        bounding_box_format=tgt_bbox_format,
        pad_to_aspect_ratio=True,
    )
    val_dataset = val_dataset.map(
        inference_resizing, num_parallel_calls=tf.data.AUTOTUNE
    )
    test_dataset = test_dataset.map(
        inference_resizing, num_parallel_calls=tf.data.AUTOTUNE
    )

    # Apply augmentations for that preserve bbox characteristics relative to image
    random_flip = keras_cv.layers.RandomFlip(
        mode="horizontal", bounding_box_format=tgt_bbox_format
    )
    # This operation randomly rescales image with a RV from distribution defined by scale_factor,
    # crops to crop_size, and then pads cropped image to target_shape
    jittered_resize = keras_cv.layers.JitteredResize(
        target_size=target_shape[0:2],
        crop_size=None,
        scale_factor=(0.85, 1.3),
        bounding_box_format=tgt_bbox_format,
    )

    train_dataset = train_dataset.map(
        random_flip, num_parallel_calls=num_parallel_calls
    ).map(jittered_resize, num_parallel_calls=num_parallel_calls)

    def conversion_wrapper(inputs):
        return convert_to_tuple(inputs, max_boxes=max_boxes)

    train_dataset = train_dataset.map(
        conversion_wrapper, num_parallel_calls=num_parallel_calls
    )
    val_dataset = val_dataset.map(
        conversion_wrapper, num_parallel_calls=tf.data.AUTOTUNE
    )
    test_dataset = test_dataset.map(
        conversion_wrapper, num_parallel_calls=tf.data.AUTOTUNE
    )

    # Fetch batches in the background while the model is training.
    train_dataset = train_dataset.prefetch(buffer_size=prefetch_buffer_size)
    val_dataset = val_dataset.prefetch(buffer_size=prefetch_buffer_size)

    return train_dataset, val_dataset, test_dataset


# Build the Keras model for object detection
def build_and_compile_detection(
    num_classes: int, bounding_box_format: str, input_shape: ty.Tuple[int, int, int]
) -> Model:
    # Load the RetinaNet architecture with EfficientNet backbone
    model = keras_cv.models.RetinaNet(
        num_classes=num_classes,
        bounding_box_format=bounding_box_format,
        # Since the input images' pixel intensities are in the range [0, 255],
        # we set include_rescaling set to True, so that the pixels are rescaled to the range [0, 1]
        backbone=keras_cv.models.EfficientNetV2Backbone.from_preset(
            "efficientnetv2_b0_imagenet",
            load_weights=True,
            include_rescaling=True,
            input_shape=input_shape,
        ),
        prediction_decoder=CombinedNMS(
            from_logits=True,
            num_classes=num_classes,
            src_bounding_box_format=bounding_box_format,
        ),
    )
    # Freeze the weights of the base model. This allows to use transfer learning
    # to train only the top layers of the model. Setting the base model to be trainable
    # would allow for all layers, not just the top, to be retrained.
    model.backbone.trainable = False

    # Enforce global clipnorm for optimizer
    optimizer = tf.keras.optimizers.SGD(
        learning_rate=0.01,
        momentum=0.9,
        global_clipnorm=10.0,
    )

    # Use preconfigured focal and smooth l1 losses
    model.compile(
        classification_loss="focal",
        box_loss="smoothl1",
        optimizer=optimizer,
    )
    return model


def save_labels(labels: ty.List[str], model_dir: str) -> None:
    filename = os.path.join(model_dir, labels_filename)
    with open(filename, "w") as f:
        for label in labels[:-1]:
            f.write(label + "\n")
        f.write(labels[-1])


def preprocessing_layers_detection(
    target_shape: ty.Tuple[int, int, int] = (256, 256, 3),
) -> ty.Tuple[tf.Tensor, tf.Tensor]:
    """Preprocessing steps to apply to all images passed through the model.
    Args:
        target_shape: intended height and width of image
    """

    preprocessing = tf.keras.Sequential(
        [
            # Resize to be (None, target_shape[0], target_shape[1], target_shape[2])
            # for compatibility with the RetinaNet model.
            keras_cv.layers.Resizing(
                target_shape[0],
                target_shape[1],
                crop_to_aspect_ratio=False,
                # Pad to aspect ratio must be true for bounding boxes.
                # This should not greatly affect the images,
                # since we're attempting to preserve aspect ratio with the above calculation.
                pad_to_aspect_ratio=True,
            ),
        ]
    )
    return preprocessing


def save_tflite_detection(
    model: Model,
    model_dir: str,
    model_name: str,
    target_shape: ty.Tuple[int, int, int],
) -> None:
    # Wrapping model here with the preprocessing step
    # This allows us to avoid overriding the custom compile function for RetinaNet
    input = tf.keras.Input(target_shape, batch_size=1, dtype=tf.uint8)
    preprocessing = preprocessing_layers_detection(target_shape=target_shape)
    predictions = model(preprocessing(input), training=False)
    # Wrap output in prediction decoder, so it's in the dictionary format that we expect.
    # Since decode_predictions relies on the input tensor for its shape,
    # we pass in a placeholder value of a tensor with ones with the intended batch and shape
    batched_prediction_placeholder = tf.ones((1,) + target_shape)
    output = model.decode_predictions(predictions, batched_prediction_placeholder)
    wrapped_model = tf.keras.Model(inputs=input, outputs=output)
    # Convert the model to tflite
    converter = tf.lite.TFLiteConverter.from_keras_model(wrapped_model)
    converter.target_spec.supported_ops = TFLITE_OPS
    # Enable default optimization to quantize model
    converter.optimizations = TFLITE_OPTIMIZATIONS
    tflite_model = converter.convert()

    filename = os.path.join(model_dir, f"{model_name}.tflite")
    # Writing the model buffer into a file.
    with open(filename, "wb") as f:
        f.write(tflite_model)


if __name__ == "__main__":
    # Set up compute device strategy
    if len(tf.config.list_physical_devices("GPU")) > 0:
        strategy = tf.distribute.OneDeviceStrategy(device="/gpu:0")
    else:
        strategy = tf.distribute.OneDeviceStrategy(device="/cpu:0")

    BATCH_SIZE = 16
    # TARGET_SHAPE is the intended shape of the model after resizing
    # For EfficientNet, this must be some multiple of 128 according to the documentation.
    TARGET_SHAPE = (384, 384, 3)
    SHUFFLE_BUFFER_SIZE = 64  # Shuffle the training data by a chunk of 64 observations
    AUTOTUNE = (
        tf.data.experimental.AUTOTUNE
    )  # Adapt preprocessing and prefetching dynamically

    # rel_yxyx indicates how we parsed the coordinates from the dataset file
    # For more information about bounding box formats, see: https://keras.io/api/keras_cv/bounding_box/formats/
    SRC_BBOX = "rel_yxyx"
    # TGT_BBOX is the format expected by the underlying keras model
    TGT_BBOX = SRC_BBOX

    # Model constants
    NUM_WORKERS = strategy.num_replicas_in_sync
    GLOBAL_BATCH_SIZE = BATCH_SIZE * NUM_WORKERS

    DATA_JSON, MODEL_DIR, num_epochs = parse_args()

    EPOCHS = 200 if num_epochs is None or 0 else int(num_epochs)
    # Read dataset file, labels should be changed according to the desired model output.
    LABELS = ["orange_triangle", "blue_star"]
    # Get filenames and bounding boxes of all images
    (
        image_filenames,
        bbox_labels,
        bbox_coords,
    ) = parse_filenames_and_bboxes_from_json(
        filename=DATA_JSON,
        all_labels=LABELS,
    )

    # Generate 80/10/10 split for train, validation and test data
    train_dataset, val_dataset, test_dataset = create_dataset_detection(
        filenames=image_filenames,
        classes=bbox_labels,
        boxes=bbox_coords,
        all_labels=LABELS,
        src_bbox_format=SRC_BBOX,
        tgt_bbox_format=TGT_BBOX,
        target_shape=TARGET_SHAPE,
        train_split=0.8,
        batch_size=GLOBAL_BATCH_SIZE,
        shuffle_buffer_size=SHUFFLE_BUFFER_SIZE,
        num_parallel_calls=AUTOTUNE,
        prefetch_buffer_size=AUTOTUNE,
    )

    # Build and compile model
    with strategy.scope():
        model = build_and_compile_detection(len(LABELS), TGT_BBOX, TARGET_SHAPE)

    # Train model on data
    loss_history = model.fit(
        x=train_dataset,
        validation_data=val_dataset,
        epochs=EPOCHS,
    )

    # Save labels.txt file
    save_labels(LABELS, MODEL_DIR)
    # Convert the model to tflite
    save_tflite_detection(model, MODEL_DIR, "detection", TARGET_SHAPE)
