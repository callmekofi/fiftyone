"""
Utilities for working with the
`ActivityNet <http://activity-net.org/index.html>`
dataset.

| Copyright 2017-2021, Voxel51, Inc.
| `voxel51.com <https://voxel51.com/>`_
|
"""
import logging
import os
import random

import youtube_dl

import eta.core.serial as etas
import eta.core.utils as etau
import eta.core.web as etaw

import fiftyone.utils.data as foud


logger = logging.getLogger(__name__)


# Possibly create Importer to add class hierarchy


def download_activitynet_split(
    dataset_dir,
    split,
    classes=None,
    num_workers=None,
    shuffle=None,
    seed=None,
    max_samples=None,
    version="200",
):
    """Utility that downloads full or partial splits of the
    `ActivityNet <http://activity-net.org/index.html>`_. dataset

    See :class:`fiftyone.types.dataset_types.ActivityNetDataset` for the
    format in which ``dataset_dir`` will be arranged.

    Args:
        dataset_dir: the directory to download the dataset
        split: the split to download. Supported values are
            ``("train", "validation", "test")``
        classes (None): a string or list of strings specifying required classes
            to load. If provided, only samples containing at least one instance
            of a specified class will be loaded
        num_workers (None): the number of processes to use when downloading
            individual video. By default, ``multiprocessing.cpu_count()`` is
            used
        shuffle (False): whether to randomly shuffle the order in which samples
            are chosen for partial downloads
        seed (None): a random seed to use when shuffling
        max_samples (None): a maximum number of samples to load per split. If
            ``classes`` are also specified, only up to the number of samples
            that contain at least one specified class will be loaded.
            By default, all matching samples are loaded
        version ("200"): the version of the ActivityNet dataset to download
            ("200", or "100")

    Returns:
        a tuple of:

        -   num_samples: the total number of downloaded videos, or ``None`` if
            everything was already downloaded
        -   classes: the list of all classes, or ``None`` if everything was
            already downloaded
        -   did_download: whether any content was downloaded (True) or if all
            necessary files were already downloaded (False)
    """
    if split not in _SPLIT_MAP.values():
        raise ValueError(
            "Unsupported split '%s'; supported values are %s"
            % (split, tuple(_SPLIT_MAP.values()))
        )

    if version not in _ANNOTATION_DOWNLOAD_LINKS:
        raise ValueError(
            "Unsupported version '%s'; supported values are %s"
            % (version, tuple(_ANNOTATION_DOWNLOAD_LINKS.keys()))
        )

    if classes is not None and split == "test":
        logger.warning("Test split is unlabeled; ignoring classes requirement")
        classes = None

    videos_dir = os.path.join(dataset_dir, "data")
    anno_path = os.path.join(dataset_dir, "labels.json")
    raw_anno_path = os.path.join(dataset_dir, "raw_labels.json")

    etau.ensure_dir(videos_dir)

    if not os.path.isfile(raw_anno_path):
        anno_link = _ANNOTATION_DOWNLOAD_LINKS[version]
        etaw.download_file(anno_link, path=raw_anno_path)

    raw_annotations = etas.load_json(raw_anno_path)

    all_classes = _get_all_classes(raw_annotations)
    target_map = {c: i for i, c in enumerate(all_classes)}

    if classes is not None:
        non_existant_classes = list(set(classes) - set(all_classes))
        if non_existant_classes:
            raise ValueError(
                "Non existant classes specified; %s"
                % tuple(non_existant_classes)
            )

    # Get ids of previously downloaded samples
    prev_downloaded_ids = _get_downloaded_sample_ids(videos_dir)

    if classes is None:
        any_class_samples = raw_annotations["database"]
        all_class_samples = {}
    else:
        # Find all samples that match either all classes specified or any
        # classes specified
        any_class_samples, all_class_samples = _get_matching_samples(
            raw_annotations, classes, split
        )

    # Check if the downloaded samples are enough, else download more
    selected_samples, num_downloaded_samples = _downloaded_necessary_samples(
        videos_dir,
        all_class_samples,
        any_class_samples,
        prev_downloaded_ids,
        max_samples,
        seed,
        shuffle,
    )

    if not num_downloaded_samples:
        num_samples = None
    else:
        num_samples = num_downloaded_samples + len(prev_downloaded_ids)

    # Save labels for this run in FiftyOneVideoClassificationDataset format
    _write_annotations(selected_samples, anno_path, target_map)

    return num_samples, all_classes


def _get_all_classes(raw_annotations):
    taxonomy = raw_annotations["taxonomy"]
    classes = set()
    parents = set()
    for node in taxonomy:
        node_name = node["nodeName"]
        parent_name = node["parentName"]
        classes.add(node_name)
        parents.add(parent_name)

    return sorted(classes - parents)


def _get_downloaded_sample_ids(videos_dir):
    video_filenames = os.listdir(videos_dir)
    video_ids = []
    for vfn in video_filenames:
        video_id, ext = os.path.splitext(vfn)
        if ext == ".part":
            logger.warning("Removing partially downloaded video %s...", vfn)
            os.remove(os.path.join(videos_dir, vfn))
        else:
            video_ids.append(video_id)

    return video_ids


def _get_matching_samples(raw_annotations, classes, split):
    # sample contains all specified classes
    all_class_match = {}

    # sample contains any specified calsses
    any_class_match = {}

    activitynet_split = _SPLIT_MAP[split]

    class_set = set(classes)
    for sample_id, annot_info in raw_annotations["database"].items():
        if activitynet_split != annot_info["subset"]:
            continue
        annot_labels = set(
            {annot["label"] for annot in annot_info["annotations"]}
        )

        if class_set.issubset(annot_labels):
            all_class_match[sample_id] = annot_info
        elif class_set & annot_labels:
            any_class_match[sample_id] = annot_info

    return any_class_match, all_class_match


def _downloaded_necessary_samples(
    videos_dir,
    all_class_samples,
    any_class_samples,
    prev_downloaded_ids,
    max_samples,
    seed,
    shuffle,
):
    all_class_ids = list(all_class_samples.keys())
    set_all_ids = set(all_class_ids)
    any_class_ids = list(any_class_samples.keys())
    set_any_ids = set(any_class_ids)

    requested_samples = {}
    requested_num = max_samples
    num_downloaded_samples = 0
    set_downloaded_ids = set(prev_downloaded_ids)

    if shuffle:
        if seed is not None:
            random.seed(seed)

    # 1) Take the all class ids that are downloaded up to max_samples
    dl_all_class_ids = list(set_all_ids.intersection(set_downloaded_ids))
    if shuffle:
        random.shuffle(dl_all_class_ids)

    add_ids = dl_all_class_ids[:requested_num]
    requested_samples.update({i: all_class_samples[i] for i in add_ids})

    if requested_num:
        requested_num -= len(add_ids)

    # 2) Take the any class ids that are downloaded up to max_samples
    if requested_num is None or requested_num:
        dl_any_class_ids = list(set_any_ids.intersection(set_downloaded_ids))
        if shuffle:
            random.shuffle(dl_any_class_ids)

        add_ids = dl_any_class_ids[:requested_num]
        requested_samples.update({i: any_class_samples[i] for i in add_ids})

        if requested_num:
            requested_num -= len(add_ids)

    # 3) Download all class ids up to max_samples
    if requested_num is None or requested_num:
        not_dl_all_class_ids = list(set_all_ids - set(dl_all_class_ids))

        if shuffle:
            random.shuffle(not_dl_all_class_ids)

        downloaded_ids = _attempt_to_download(
            videos_dir, not_dl_all_class_ids, all_class_samples, requested_num
        )
        num_downloaded_samples += len(downloaded_ids)
        requested_samples.update(
            {i: all_class_samples[i] for i in downloaded_ids}
        )

        if requested_num:
            requested_num -= len(downloaded_ids)

    # 4) Download any class ids up to max_samples
    if requested_num is None or requested_num:
        not_dl_any_class_ids = list(set_any_ids - set(dl_any_class_ids))

        if shuffle:
            random.shuffle(not_dl_any_class_ids)

        downloaded_ids = _attempt_to_download(
            videos_dir, not_dl_any_class_ids, any_class_samples, requested_num
        )
        num_downloaded_samples += len(downloaded_ids)
        requested_samples.update(
            {i: any_class_samples[i] for i in downloaded_ids}
        )

    return requested_samples, num_downloaded_samples


def _attempt_to_download(videos_dir, ids, samples_info, num_samples):
    downloaded = []
    for sample_id in ids:
        sample_info = samples_info[sample_id]
        url = sample_info["url"]
        output_path = os.path.join(videos_dir, "%s.mp4" % sample_id)
        success, error = _do_download(url, output_path)
        if not success:
            continue
        else:
            downloaded.append(sample_id)
            if num_samples is not None and len(downloaded) >= num_samples:
                return downloaded
    return downloaded


def _do_download(url, output_path):
    try:
        ydl_opts = {"outtmpl": output_path}
        with youtube_dl.YoutubeDL(ydl_opts) as ydl:
            ydl.download([url])

        return True, None
    except Exception as e:
        return False, e


def _write_annotations(matching_samples, anno_path, target_map):
    fo_matching_labels = _convert_label_format(matching_samples, target_map)
    etas.write_json(fo_matching_labels, anno_path)


def _convert_label_format(activitynet_labels, target_map):
    labels = {}
    for annot_id, annot_info in activitynet_labels.items():
        fo_annot_labels = []
        for an_annot_label in annot_info["annotations"]:
            target = target_map[an_annot_label["label"]]
            timestamps = an_annot_label["segment"]
            fo_annot_labels.append({"label": target, "timestamps": timestamps})

        labels[annot_id] = fo_annot_labels

    fo_annots = {"classes": list(target_map.keys()), "labels": labels}
    return fo_annots


_ANNOTATION_DOWNLOAD_LINKS = {
    "200": "http://ec2-52-25-205-214.us-west-2.compute.amazonaws.com/files/activity_net.v1-3.min.json",
    "100": "http://ec2-52-25-205-214.us-west-2.compute.amazonaws.com/files/activity_net.v1-2.min.json",
}

_SPLIT_MAP = {
    "training": "train",
    "testing": "test",
    "validation": "validation",
}