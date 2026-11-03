"""Held-out evaluation for the frozen logistic-regression classifier.

Loads a labeled sample archive (default ``Model/classifier.sample.npz``), runs
the *same* scoring used by ``Model.inference.predict`` (standardize -> coef dot
product -> sigmoid -> 0.5 threshold), and reports per-class precision/recall
plus a ``defect_probability`` histogram.

The point of this script is to separate two failure modes that look similar
from the outside:

* genuine overfitting  -> weak accuracy on unseen *same-distribution* (synthetic)
  data, with many confident-but-wrong scores pinned near 0 or 1;
* synthetic-vs-real distribution shift -> strong accuracy on this synthetic
  held-out sample, so any bad real-factory predictions come from domain mismatch,
  not from the model memorizing the training set.

No Django or scikit-learn dependency: numpy + Pillow only, matching the frozen
model's own runtime.
"""
from __future__ import annotations

import argparse
from pathlib import Path

import numpy as np
from PIL import Image

from Model.inference import CLASS_NAMES, MODEL_VERSION, features

NORMAL, DEFECTIVE = 0, 1          # label index -> class name (matches metadata)
# Threshold and clipping mirror inference.predict exactly.
NEAR = {"tight": 0.02, "loose": 0.05}   # "stuck" bands around the 0 / 1 ends


def _load_arrays(path: Path):
    """Read the sample npz and return (X, y, image_paths, images_root).

    Supported key conventions:
      * features: ``x`` / ``X`` / ``features``  (n, 17) float
      * labels:   ``y`` / ``labels`` / ``targets`` (n,) int or class-name str
      * images:   ``image_paths`` / ``images`` (n,) str, resolved against an
                  optional ``images_root`` (str) key or the --images-root flag
    """
    if not path.exists():
        raise SystemExit(
            f"Sample file not found: {path}\n"
            "Create it as an .npz holding either precomputed features "
            "(keys 'x','y') or image paths (keys 'image_paths','y'). "
            "See the module docstring."
        )
    with np.load(path, allow_pickle=True) as data:
        keys = set(data.files)

        def pick(*names):
            for n in names:
                if n in keys:
                    return data[n]
            return None

        X = pick("x", "X", "features")
        y = pick("y", "labels", "targets")
        image_paths = pick("image_paths", "images", "paths")
        images_root = pick("images_root", "root")
    if y is None:
        raise SystemExit(f"Sample file {path.name} has no labels "
                         "(expected one of: y / labels / targets).")
    return X, np.asarray(y).ravel(), image_paths, images_root


def _labels_to_index(y):
    """Map int labels or class-name strings to 0/1; raise on anything else."""
    out = np.empty(y.shape[0], dtype=int)
    for i, v in enumerate(y):
        s = str(v).strip().lower()
        if s in {"0", "normal"}:
            out[i] = NORMAL
        elif s in {"1", "defective", "defect"}:
            out[i] = DEFECTIVE
        else:
            raise SystemExit(f"Unrecognized label at index {i}: {v!r}")
    return out


def _features_from_paths(image_paths, images_root, override_root):
    root = Path(override_root) if override_root else (
        Path(str(images_root)) if images_root is not None else Path.cwd())
    rows = []
    for i, rel in enumerate(image_paths):
        p = (root / str(rel)) if not Path(str(rel)).is_absolute() else Path(str(rel))
        if not p.exists():
            raise SystemExit(f"Image missing for sample {i}: {p}")
        rows.append(features(Image.open(p)))
    return np.asarray(rows, dtype=np.float64)


def score(X, model_path: Path):
    """Apply inference.py's logistic model to feature rows; return defect p."""
    with np.load(model_path, allow_pickle=False) as model:
        z = ((X - model["mean"]) / model["scale"]) @ model["coef"] + model["intercept"].item()
    return 1.0 / (1.0 + np.exp(-np.clip(z, -50, 50)))


def _precision_recall(y_true, y_pred, positive):
    tp = int(np.sum((y_pred == positive) & (y_true == positive)))
    fp = int(np.sum((y_pred == positive) & (y_true != positive)))
    fn = int(np.sum((y_pred != positive) & (y_true == positive)))
    precision = tp / (tp + fp) if (tp + fp) else 0.0
    recall = tp / (tp + fn) if (tp + fn) else 0.0
    f1 = 2 * precision * recall / (precision + recall) if (precision + recall) else 0.0
    return precision, recall, f1, tp, fp, fn


def _bar(frac, width=30):
    return "#" * int(round(frac * width))


def report(p, y_true, model_version):
    y_pred = (p >= 0.5).astype(int)
    n = p.shape[0]
    correct = y_pred == y_true

    print(f"Model version : {model_version}")
    print(f"Samples       : {n} "
          f"(Normal={int((y_true == NORMAL).sum())}, "
          f"Defective={int((y_true == DEFECTIVE).sum())})")
    accuracy = correct.mean() if n else 0.0
    print(f"Accuracy      : {accuracy:.3f}\n")

    # Confusion matrix -----------------------------------------------------
    print("Confusion matrix (rows=true, cols=pred)")
    print(f"{'':>12}{'pred N':>10}{'pred D':>10}")
    tn = int(np.sum((y_true == NORMAL) & (y_pred == NORMAL)))
    fp_n = int(np.sum((y_true == NORMAL) & (y_pred == DEFECTIVE)))
    fn_d = int(np.sum((y_true == DEFECTIVE) & (y_pred == NORMAL)))
    tp_d = int(np.sum((y_true == DEFECTIVE) & (y_pred == DEFECTIVE)))
    print(f"{'true N':>12}{tn:>10}{fp_n:>10}")
    print(f"{'true D':>12}{fn_d:>10}{tp_d:>10}\n")

    # Per-class precision / recall ---------------------------------------
    print("Per-class metrics")
    for positive, name in ((NORMAL, "Normal"), (DEFECTIVE, "Defective")):
        pr, rc, f1, tp, fp, fn = _precision_recall(y_true, y_pred, positive)
        print(f"  {name:<10} precision={pr:.3f}  recall={rc:.3f}  f1={f1:.3f} "
              f"(tp={tp} fp={fp} fn={fn})")

    # Histogram of defect_probability ------------------------------------
    print("\ndefect_probability histogram (10 bins)")
    counts, edges = np.histogram(p, bins=10, range=(0.0, 1.0))
    for i, c in enumerate(counts):
        lo, hi = edges[i], edges[i + 1]
        print(f"  [{lo:.1f},{hi:.1f}) {c:>4}  {_bar(c / n if n else 0)}")

    # Stuck-score diagnosis ----------------------------------------------
    near0 = int(np.sum(p < NEAR["loose"]))
    near1 = int(np.sum(p > 1 - NEAR["loose"]))
    v0 = int(np.sum(p < NEAR["tight"]))
    v1 = int(np.sum(p > 1 - NEAR["tight"]))
    confident = np.abs(p - 0.5) > 0.45            # p < .05 or p > .95
    confident_wrong = int(np.sum(confident & ~correct))
    stuck_frac = (near0 + near1) / n if n else 0.0

    print("\nScore-pinning diagnosis")
    print(f"  near 0/1 (<0.05 or >0.95) : {near0 + near1}/{n} ({stuck_frac:.1%})")
    print(f"  very stuck (<0.02/>0.98) : {v0 + v1}/{n}")
    print(f"  confident-but-wrong       : {confident_wrong}/{n} "
          f"({(confident_wrong / n if n else 0):.1%})")
    mean_margin = float(np.mean(np.abs(p - 0.5))) if n else 0.0
    print(f"  mean |p-0.5|             : {mean_margin:.3f}\n")

    # Heuristic verdict ---------------------------------------------------
    print("Interpretation (heuristic, confirm with training-set accuracy):")
    if accuracy >= 0.9 and confident_wrong <= 0.05 * n:
        print("  * Strong accuracy on unseen SAME-distribution (synthetic) data "
              "with few confident errors.")
        print("    -> This is NOT overfitting. Bad real-factory predictions "
              "indicate synthetic-vs-real distribution shift.")
    elif accuracy < 0.8 and (confident_wrong > 0.1 * n or stuck_frac > 0.5):
        print("  * Weak accuracy on unseen synthetic data + many confident-but-"
              "wrong / pinned scores.")
        print("    -> Consistent with overfitting / overconfidence to the "
              "synthetic recipe (high variance, poor calibration).")
    else:
        print("  * Mixed signal. Compare against training-set accuracy: a large "
              "train-vs-sample gap => overfitting; similar (low) both => "
              "underfitting / capacity, not overfitting.")


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__,
                                     formatter_class=argparse.RawDescriptionHelpFormatter)
    here = Path(__file__).resolve().parent
    parser.add_argument("--sample", default=str(here / "classifier.sample.npz"),
                        help="Labeled sample .npz (default: Model/classifier.sample.npz)")
    parser.add_argument("--model", default=str(here / "classifier.npz"),
                        help="Trained model .npz (default: Model/classifier.npz)")
    parser.add_argument("--images-root", default=None,
                        help="Base dir for image_paths when the sample stores paths")
    args = parser.parse_args(argv)

    sample_path, model_path = Path(args.sample), Path(args.model)
    X, y_raw, image_paths, images_root = _load_arrays(sample_path)
    y_true = _labels_to_index(y_raw)

    if X is None:
        if image_paths is None:
            raise SystemExit("Sample has neither features (x) nor image_paths; "
                             "cannot run the model.")
        X = _features_from_paths(np.asarray(image_paths).ravel(), images_root,
                                 args.images_root)
    X = np.asarray(X, dtype=np.float64)

    if X.shape[0] != y_true.shape[0]:
        raise SystemExit(f"Feature count {X.shape[0]} != label count {y_true.shape[0]}")

    p = score(X, model_path)
    report(p, y_true, MODEL_VERSION)


if __name__ == "__main__":
    main()
