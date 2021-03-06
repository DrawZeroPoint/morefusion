#!/usr/bin/env python

import argparse
import json

import chainer
import cupy
import numpy as np
import pandas
import path
import pybullet  # NOQA
import tqdm

import morefusion
from morefusion.contrib import singleview_3d


models = morefusion.datasets.YCBVideoModels()


def main():
    parser = argparse.ArgumentParser(
        formatter_class=argparse.ArgumentDefaultsHelpFormatter,
    )
    parser.add_argument("log_dir", type=path.Path, help="log dir")
    args = parser.parse_args()

    # args.log_dir = path.Path(
    #     "./a.data/logs.20191008.all_data/20191014_092021.638983636"
    # )  # NOQA

    with open(args.log_dir / "args.json") as f:
        args_dict = json.load(f)

    model = singleview_3d.models.Model(
        n_fg_class=len(args_dict["class_names"][1:]),
        pretrained_resnet18=args_dict["pretrained_resnet18"],
        with_occupancy=args_dict["with_occupancy"],
    )
    assert args_dict["pretrained_resnet18"] is True
    assert args_dict["with_occupancy"] is True
    chainer.serializers.load_npz(
        args.log_dir / "snapshot_model_best_add.npz", model
    )
    model.to_gpu(0)

    dataset = morefusion.datasets.MySyntheticYCB20190916RGBDPoseEstimationDatasetReIndexed(  # NOQA
        split="val", class_ids=args_dict["class_ids"], version=1,
    )

    def transform(in_data):
        grid_target = in_data.pop("grid_target") > 0.5
        grid_nontarget = in_data.pop("grid_nontarget") > 0.5
        grid_empty = in_data.pop("grid_empty") > 0.5
        grid_nontarget = grid_nontarget ^ grid_target
        grid_empty = grid_empty ^ grid_target

        grid_target_full = in_data.pop("grid_target_full")
        assert np.isin(grid_target_full, [0, 1]).all()
        grid_target_full = grid_target_full.astype(bool)

        grid_nontarget_full = in_data.pop("grid_nontarget_full")
        nontarget_ids = np.unique(grid_nontarget_full)
        nontarget_ids = nontarget_ids[nontarget_ids > 0]
        if len(nontarget_ids) > 0:
            grid_nontarget_full = np.isin(grid_nontarget_full, nontarget_ids)
        else:
            grid_nontarget_full = np.zeros_like(grid_target)
        grid_nontarget_full = grid_nontarget_full ^ grid_target_full

        # grid_nontarget_empty = grid_nontarget_full | grid_empty
        grid_nontarget_empty = ~grid_target_full

        in_data["grid_target"] = grid_target
        in_data["grid_nontarget_empty"] = grid_nontarget_empty
        return in_data

    dataset = chainer.datasets.TransformDataset(dataset, transform)

    data = []
    for index in tqdm.trange(len(dataset)):
        examples = [dataset.get_example(index)]

        batch = chainer.dataset.concat_examples(examples, device=0)
        with chainer.no_backprop_mode(), chainer.using_config("train", False):
            quaternion, translation, confidence = model.predict(
                class_id=batch["class_id"],
                rgb=batch["rgb"],
                pcd=batch["pcd"],
                pitch=batch["pitch"],
                origin=batch["origin"],
                grid_nontarget_empty=batch["grid_nontarget_empty"],
            )
        indices = model.xp.argmax(confidence.array, axis=1)
        quaternion = quaternion[model.xp.arange(len(examples)), indices]
        translation = translation[model.xp.arange(len(examples)), indices]

        transform = morefusion.functions.transformation_matrix(
            chainer.cuda.to_cpu(quaternion.array),
            chainer.cuda.to_cpu(translation.array),
        ).array

        transform_true = morefusion.functions.transformation_matrix(
            batch["quaternion_true"], batch["translation_true"]
        ).array
        transform_true = chainer.cuda.to_cpu(transform_true)

        # visualization
        """
        import trimesh
        frame = dataset._dataset.get_frame(index)
        scene = trimesh.Scene()
        scene_true = trimesh.Scene(camera=scene.camera)
        for i in range(len(examples)):
            class_id = examples[i]['class_id']
            cad = models.get_cad(class_id)
            if hasattr(cad.visual, 'to_color'):
                cad.visual = cad.visual.to_color()
            scene.add_geometry(cad, transform=transform[i])
            scene_true.add_geometry(cad, transform=transform_true[i])
        scene.camera_transform = morefusion.extra.trimesh.to_opengl_transform()
        scenes = {'pose': scene, 'pose_true': scene_true, 'rgb': frame['rgb']}
        morefusion.extra.trimesh.display_scenes(scenes, tile=(1, 3))
        """

        # add result w/ occupancy
        for i in range(len(examples)):
            points = models.get_pcd(class_id=examples[i]["class_id"])
            add, add_s = morefusion.metrics.average_distance(
                [points], transform_true[i : i + 1], transform[i : i + 1]
            )
            add, add_s = add[0], add_s[0]
            if (
                examples[i]["class_id"]
                in morefusion.datasets.ycb_video.class_ids_symmetric
            ):  # NOQA
                add_or_add_s = add_s
            else:
                add_or_add_s = add
            data.append(
                {
                    "frame_index": index,
                    "batch_index": i,
                    "class_id": examples[i]["class_id"],
                    "add_or_add_s": add_or_add_s,
                    "add_s": add_s,
                    # "visibility": examples[i]["visibility"],
                    "method": "morefusion",
                }
            )

        """
        transform_icp = iterative_closest_point(examples, batch, transform)

        for i in range(len(examples)):
            points = models.get_pcd(class_id=examples[i]["class_id"])
            add, add_s = morefusion.metrics.average_distance(
                [points], transform_true[i : i + 1], transform_icp[i : i + 1]
            )
            add, add_s = add[0], add_s[0]
            if (
                examples[i]["class_id"]
                in morefusion.datasets.ycb_video.class_ids_symmetric
            ):  # NOQA
                add_or_add_s = add_s
            else:
                add_or_add_s = add
            data.append(
                {
                    "frame_index": index,
                    "batch_index": i,
                    "class_id": examples[i]["class_id"],
                    "add_or_add_s": add_or_add_s,
                    "add_s": add_s,
                    "visibility": examples[i]["visibility"],
                    "method": "morefusion+icp",
                }
            )

        transform_icc = iterative_collision_check(examples, batch, transform)

        for i in range(len(examples)):
            points = models.get_pcd(class_id=examples[i]["class_id"])
            add, add_s = morefusion.metrics.average_distance(
                [points], transform_true[i : i + 1], transform_icc[i : i + 1]
            )
            add, add_s = add[0], add_s[0]
            if (
                examples[i]["class_id"]
                in morefusion.datasets.ycb_video.class_ids_symmetric
            ):  # NOQA
                add_or_add_s = add_s
            else:
                add_or_add_s = add
            data.append(
                {
                    "frame_index": index,
                    "batch_index": i,
                    "class_id": examples[i]["class_id"],
                    "add_or_add_s": add_or_add_s,
                    "add_s": add_s,
                    "visibility": examples[i]["visibility"],
                    "method": "morefusion+icc",
                }
            )

        transform_icc_icp = iterative_closest_point(
            examples, batch, transform_icc, n_iteration=30
        )

        for i in range(len(examples)):
            points = models.get_pcd(class_id=examples[i]["class_id"])
            add, add_s = morefusion.metrics.average_distance(
                [points],
                transform_true[i : i + 1],
                transform_icc_icp[i : i + 1],
            )
            add, add_s = add[0], add_s[0]
            if (
                examples[i]["class_id"]
                in morefusion.datasets.ycb_video.class_ids_symmetric
            ):  # NOQA
                add_or_add_s = add_s
            else:
                add_or_add_s = add
            data.append(
                {
                    "frame_index": index,
                    "batch_index": i,
                    "class_id": examples[i]["class_id"],
                    "add_or_add_s": add_or_add_s,
                    "add_s": add_s,
                    "visibility": examples[i]["visibility"],
                    "method": "morefusion+icc+icp",
                }
            )
        """

    df = pandas.DataFrame(data)
    df.to_csv(args.log_dir / "evaluate.csv")


def iterative_closest_point(examples, batch, transform, n_iteration=100):
    transform_icp = []
    for i in range(len(examples)):
        nonnan = ~np.isnan(examples[i]["pcd"]).any(axis=2)
        icp = morefusion.contrib.ICPRegistration(
            examples[i]["pcd"][nonnan],
            models.get_pcd(class_id=examples[i]["class_id"]),
            transform[i],
        )
        transform_i = icp.register(iteration=n_iteration)
        transform_icp.append(transform_i)
    return np.array(transform_icp, dtype=np.float32)


def iterative_collision_check(examples, batch, transform):
    # refine with occupancy
    link = morefusion.contrib.IterativeCollisionCheckLink(transform)
    link.to_gpu()
    optimizer = chainer.optimizers.Adam(alpha=0.01)
    optimizer.setup(link)
    link.translation.update_rule.hyperparam.alpha *= 0.1
    #
    points = []
    sdfs = []
    for i in range(len(examples)):
        pcd, sdf = models.get_sdf(examples[i]["class_id"])
        keep = ~np.isnan(sdf)
        pcd, sdf = pcd[keep], sdf[keep]
        points.append(cupy.asarray(pcd, dtype=np.float32))
        sdfs.append(cupy.asarray(sdf, dtype=np.float32))
    #
    for i in range(30):
        loss = link(
            points,
            sdfs,
            batch["pitch"].astype(np.float32),
            batch["origin"].astype(np.float32),
            batch["grid_target"].astype(np.float32),
            batch["grid_nontarget_empty"].astype(np.float32),
        )
        loss.backward()
        optimizer.update()
        link.zerograds()
    #
    transform = morefusion.functions.transformation_matrix(
        link.quaternion, link.translation
    ).array
    transform = chainer.cuda.to_cpu(transform)
    return transform


if __name__ == "__main__":
    main()
