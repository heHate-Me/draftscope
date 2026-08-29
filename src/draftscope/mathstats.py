from __future__ import annotations

import math
from typing import Iterable, Sequence


def clean(values: Iterable[float | int | None]) -> list[float]:
    result: list[float] = []
    for value in values:
        if value is None:
            continue
        number = float(value)
        if math.isfinite(number):
            result.append(number)
    return result


def mean(values: Iterable[float | int | None]) -> float | None:
    items = clean(values)
    return sum(items) / len(items) if items else None


def quantile(values: Iterable[float | int | None], probability: float) -> float | None:
    items = sorted(clean(values))
    if not items:
        return None
    if len(items) == 1:
        return items[0]
    probability = min(1.0, max(0.0, probability))
    index = probability * (len(items) - 1)
    low = math.floor(index)
    high = math.ceil(index)
    if low == high:
        return items[low]
    fraction = index - low
    return items[low] * (1.0 - fraction) + items[high] * fraction


def median(values: Iterable[float | int | None]) -> float | None:
    return quantile(values, 0.5)


def variance(values: Iterable[float | int | None], *, sample: bool = False) -> float | None:
    items = clean(values)
    divisor = len(items) - 1 if sample else len(items)
    if divisor <= 0:
        return None
    center = sum(items) / len(items)
    return sum((item - center) ** 2 for item in items) / divisor


def stdev(values: Iterable[float | int | None], *, sample: bool = False) -> float | None:
    value = variance(values, sample=sample)
    return math.sqrt(value) if value is not None else None


def mad(values: Iterable[float | int | None]) -> float | None:
    items = clean(values)
    center = median(items)
    if center is None:
        return None
    return median(abs(item - center) for item in items)


def iqr(values: Iterable[float | int | None]) -> float | None:
    low = quantile(values, 0.25)
    high = quantile(values, 0.75)
    if low is None or high is None:
        return None
    return high - low


def robust_scale(values: Iterable[float | int | None]) -> float:
    items = clean(values)
    robust = mad(items)
    if robust is not None and robust > 1e-9:
        return 1.4826 * robust
    spread = stdev(items)
    return spread if spread is not None and spread > 1e-9 else 1.0


def percentile_rank(value: float, values: Iterable[float | int | None]) -> float | None:
    """Empirical midrank percentile in [0, 100]."""
    items = clean(values)
    if not items:
        return None
    less = sum(item < value for item in items)
    equal = sum(item == value for item in items)
    return 100.0 * (less + 0.5 * equal) / len(items)


def metric_score(value: float, values: Iterable[float | int | None], direction: str, *, target: float | None = None) -> float | None:
    items = clean(values)
    if not items:
        return None
    rank = percentile_rank(value, items)
    if rank is None:
        return None
    if direction == "higher":
        return rank
    if direction == "lower":
        return 100.0 - rank
    if direction == "target":
        center = target if target is not None else median(items)
        if center is None:
            return None
        z = abs(value - center) / robust_scale(items)
        return 100.0 * math.exp(-0.5 * (z / 1.75) ** 2)
    return rank


def weighted_mean(pairs: Iterable[tuple[float | None, float]]) -> float | None:
    numerator = 0.0
    denominator = 0.0
    for value, weight in pairs:
        if value is None or weight <= 0:
            continue
        numerator += float(value) * weight
        denominator += weight
    return numerator / denominator if denominator else None


def sigmoid(value: float) -> float:
    if value >= 0:
        z = math.exp(-min(value, 700.0))
        return 1.0 / (1.0 + z)
    z = math.exp(max(value, -700.0))
    return z / (1.0 + z)


def logit(probability: float) -> float:
    p = min(1.0 - 1e-9, max(1e-9, probability))
    return math.log(p / (1.0 - p))


def brier_score(labels: Sequence[int], probabilities: Sequence[float]) -> float | None:
    if not labels or len(labels) != len(probabilities):
        return None
    return sum((float(label) - probability) ** 2 for label, probability in zip(labels, probabilities)) / len(labels)


def log_loss(labels: Sequence[int], probabilities: Sequence[float]) -> float | None:
    if not labels or len(labels) != len(probabilities):
        return None
    total = 0.0
    for label, probability in zip(labels, probabilities):
        p = min(1.0 - 1e-12, max(1e-12, probability))
        total -= label * math.log(p) + (1 - label) * math.log(1 - p)
    return total / len(labels)


def roc_auc(labels: Sequence[int], probabilities: Sequence[float]) -> float | None:
    positives = sum(labels)
    negatives = len(labels) - positives
    if positives == 0 or negatives == 0:
        return None
    ordered = sorted(zip(probabilities, labels), key=lambda pair: pair[0])
    rank_sum = 0.0
    index = 0
    while index < len(ordered):
        end = index + 1
        while end < len(ordered) and ordered[end][0] == ordered[index][0]:
            end += 1
        average_rank = ((index + 1) + end) / 2.0
        rank_sum += average_rank * sum(label for _, label in ordered[index:end])
        index = end
    return (rank_sum - positives * (positives + 1) / 2.0) / (positives * negatives)


def average_precision(labels: Sequence[int], probabilities: Sequence[float]) -> float | None:
    positives = sum(labels)
    if positives == 0:
        return None
    ranked = sorted(zip(probabilities, labels), key=lambda pair: pair[0], reverse=True)
    hits = 0
    total = 0.0
    for rank, (_, label) in enumerate(ranked, start=1):
        if label:
            hits += 1
            total += hits / rank
    return total / positives


def f1_threshold(labels: Sequence[int], probabilities: Sequence[float]) -> tuple[float, float]:
    if not labels:
        return 0.5, 0.0
    candidates = sorted({round(p, 6) for p in probabilities} | {0.5})
    best_threshold = 0.5
    best_score = -1.0
    for threshold in candidates:
        tp = sum(label == 1 and probability >= threshold for label, probability in zip(labels, probabilities))
        fp = sum(label == 0 and probability >= threshold for label, probability in zip(labels, probabilities))
        fn = sum(label == 1 and probability < threshold for label, probability in zip(labels, probabilities))
        denominator = 2 * tp + fp + fn
        score = 2 * tp / denominator if denominator else 0.0
        if score > best_score or (score == best_score and abs(threshold - 0.5) < abs(best_threshold - 0.5)):
            best_threshold, best_score = threshold, score
    return best_threshold, best_score
