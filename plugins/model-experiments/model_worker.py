#!/usr/bin/env python3
"""Reproducible grouped, rolling and baseline model experiments."""
from __future__ import annotations

import csv
import hashlib
import json
import math
import sys
import uuid
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

import numpy as np
import pandas as pd
from sklearn.base import clone
from sklearn.ensemble import RandomForestClassifier, RandomForestRegressor
from sklearn.inspection import permutation_importance
from sklearn.linear_model import LinearRegression, LogisticRegression, Ridge
from sklearn.metrics import accuracy_score, f1_score, mean_absolute_error, mean_squared_error, precision_score, r2_score, recall_score, roc_auc_score
from sklearn.model_selection import GroupKFold, KFold, StratifiedKFold

MAX_STDIN_BYTES = 4 * 1024 * 1024


class ModelError(ValueError):
    pass


def inside(path: Path, root: Path) -> bool:
    return path == root or root in path.parents


def project_root(request: dict[str, Any]) -> Path:
    raw = request.get('projectRoot')
    allowed = request.get('allowedRoots')
    if not isinstance(raw, str) or not raw.strip():
        raise ModelError('projectRoot is required for local files')
    if not isinstance(allowed, list) or not allowed:
        raise ModelError('allowedRoots is required')
    root = Path(raw).expanduser().resolve()
    if not root.is_dir():
        raise ModelError(f'Project root is not a directory: {root}')
    for item in allowed:
        if isinstance(item, str) and inside(root, Path(item).expanduser().resolve()):
            return root
    raise ModelError(f'Project root is outside allowedRoots: {root}')


def state_root(request: dict[str, Any]) -> Path:
    raw = request.get('stateRoot')
    if not isinstance(raw, str) or not Path(raw).is_absolute():
        raise ModelError('stateRoot must be an absolute path supplied by plugin config')
    root = Path(raw).expanduser().resolve()
    root.mkdir(parents=True, exist_ok=True)
    return root


def local_path(root: Path, raw: Any, state: Path | None = None) -> Path:
    if not isinstance(raw, str) or not raw.strip():
        raise ModelError('File path must be a non-empty string')
    if raw.startswith('state://'):
        if state is None:
            raise ModelError('stateRoot is required for state:// paths')
        path = (state / raw[len('state://'):]).resolve()
        if not inside(path, state):
            raise ModelError('State file escapes stateRoot')
        return path
    candidate = Path(raw)
    path = candidate.resolve() if candidate.is_absolute() else (root / candidate).resolve()
    if state is not None and inside(path, state):
        return path
    if not inside(path, root):
        raise ModelError(f'File escapes projectRoot: {raw}')
    if not path.is_file():
        raise ModelError(f'File not found: {raw}')
    return path


def number(request: dict[str, Any], key: str, default: float, minimum: float, maximum: float) -> float:
    value = request.get(key, default)
    if isinstance(value, bool) or not isinstance(value, (int, float)) or not np.isfinite(value) or value < minimum or value > maximum:
        raise ModelError(f'{key} must be a finite number from {minimum} to {maximum}')
    return float(value)


def hash_file(path: Path, max_bytes: int) -> dict[str, Any]:
    size = path.stat().st_size
    if size > max_bytes:
        return {'path': str(path), 'bytes': size, 'skipped': True}
    digest = hashlib.sha256()
    with path.open('rb') as stream:
        for chunk in iter(lambda: stream.read(1024 * 1024), b''):
            digest.update(chunk)
    return {'path': str(path), 'bytes': size, 'sha256': digest.hexdigest()}


def json_safe(value: Any) -> Any:
    if isinstance(value, float) and not math.isfinite(value):
        return None
    if isinstance(value, np.generic):
        return json_safe(value.item())
    if isinstance(value, np.ndarray):
        return json_safe(value.tolist())
    if isinstance(value, dict):
        return {key: json_safe(item) for key, item in value.items()}
    if isinstance(value, (list, tuple)):
        return [json_safe(item) for item in value]
    if isinstance(value, Path):
        return str(value)
    if isinstance(value, (pd.Timestamp, datetime)):
        return value.isoformat()
    return value


def prepare_data(request: dict[str, Any]) -> tuple[Path, pd.DataFrame, list[str], str, str, str | None]:
    path = local_path(project_root(request), request.get('table'), state_root(request))
    frame = pd.read_csv(path)
    frame.columns = [str(column) for column in frame.columns]
    features = request.get('featureColumns')
    if not isinstance(features, list) or not features or any(not isinstance(item, str) for item in features):
        raise ModelError('featureColumns must be a non-empty string array')
    target = request.get('targetColumn')
    task = request.get('task', 'classification')
    if not isinstance(target, str) or target not in frame.columns:
        raise ModelError('targetColumn is required and must exist')
    if task not in {'classification', 'regression'}:
        raise ModelError('task must be classification or regression')
    if any(feature not in frame.columns for feature in features):
        raise ModelError('One or more featureColumns are missing')
    if target in features:
        raise ModelError('targetColumn must not be included in featureColumns')
    group = request.get('groupColumn')
    if group is not None and (not isinstance(group, str) or group not in frame.columns):
        raise ModelError('groupColumn must exist when provided')
    extra = [group] if group else []
    order_column = request.get('orderColumn')
    if isinstance(order_column, str) and order_column not in features + [target] + extra:
        extra.append(order_column)
    data = frame[features + [target] + extra].copy()
    for feature in features:
        data[feature] = pd.to_numeric(data[feature], errors='coerce')
    if task == 'regression':
        data[target] = pd.to_numeric(data[target], errors='coerce')
    data = data.replace([np.inf, -np.inf], np.nan).dropna()
    if len(data) < 10:
        raise ModelError('At least 10 complete observations are required')
    return path, data, features, target, task, group


def make_model(request: dict[str, Any], task: str):
    model_name = request.get('model', 'random_forest')
    random_state = int(number(request, 'randomState', 42, 0, 100000000))
    if task == 'classification':
        if model_name == 'random_forest':
            return RandomForestClassifier(n_estimators=int(number(request, 'nEstimators', 100, 1, 1000)), max_depth=request.get('maxDepth'), random_state=random_state, n_jobs=1)
        if model_name == 'logistic':
            return LogisticRegression(max_iter=1000, random_state=random_state)
        raise ModelError(f'Unsupported classification model: {model_name}')
    if model_name == 'random_forest':
        return RandomForestRegressor(n_estimators=int(number(request, 'nEstimators', 100, 1, 1000)), max_depth=request.get('maxDepth'), random_state=random_state, n_jobs=1)
    if model_name == 'ridge':
        return Ridge()
    if model_name == 'linear':
        return LinearRegression()
    raise ModelError(f'Unsupported regression model: {model_name}')


def score_predictions(y_true, y_pred, score, task: str, positive_label: str | None) -> dict[str, Any]:
    if task == 'regression':
        return {'mae': float(mean_absolute_error(y_true, y_pred)), 'rmse': float(np.sqrt(mean_squared_error(y_true, y_pred))), 'r2': float(r2_score(y_true, y_pred))}
    positive = np.asarray(y_true) == positive_label
    predicted_positive = np.asarray(y_pred) == positive_label
    result = {
        'accuracy': float(accuracy_score(y_true, y_pred)),
        'precision': float(precision_score(positive, predicted_positive, zero_division=0)),
        'recall': float(recall_score(positive, predicted_positive, zero_division=0)),
        'f1': float(f1_score(positive, predicted_positive, zero_division=0)),
    }
    if score is not None and len(np.unique(positive)) > 1:
        result['rocAuc'] = float(roc_auc_score(positive, score))
        result['brier'] = float(np.mean((np.asarray(score) - positive.astype(float)) ** 2))
    return result


def predict_model(model, features: pd.DataFrame, task: str, positive_label: str | None):
    prediction = model.predict(features)
    score = None
    if task == 'classification' and hasattr(model, 'predict_proba'):
        probabilities = model.predict_proba(features)
        classes = list(model.classes_)
        if positive_label in classes:
            score = probabilities[:, classes.index(positive_label)]
    return prediction, score


def split_indices(data: pd.DataFrame, features: list[str], target: str, task: str, group: str | None, folds: int):
    if group:
        if data[group].nunique() < 2:
            raise ModelError('Grouped validation requires at least two groups')
        splitter = GroupKFold(n_splits=min(folds, data[group].nunique()))
        return splitter.split(data[features], data[target], data[group])
    if task == 'classification':
        if data[target].nunique() < 2:
            raise ModelError('Classification requires at least two classes')
        splitter = StratifiedKFold(n_splits=min(folds, data[target].value_counts().min()), shuffle=True, random_state=42)
        return splitter.split(data[features], data[target])
    splitter = KFold(n_splits=folds, shuffle=True, random_state=42)
    return splitter.split(data[features])


def evaluate(request: dict[str, Any]) -> dict[str, Any]:
    path, data, features, target, task, group = prepare_data(request)
    positive_label = request.get('positiveLabel')
    if task == 'classification' and not isinstance(positive_label, str):
        raise ModelError('positiveLabel is required for classification')
    folds = int(number(request, 'cvFolds', 5, 2, 20))
    predictions = []
    fold_metrics = []
    duplicate_feature_rows = 0
    group_overlap = 0
    for fold, (train_index, test_index) in enumerate(split_indices(data, features, target, task, group, folds), start=1):
        train = data.iloc[train_index]
        test = data.iloc[test_index]
        model = make_model(request, task)
        model.fit(train[features], train[target])
        prediction, score = predict_model(model, test[features], task, positive_label)
        metrics = score_predictions(test[target].to_numpy(), prediction, score, task, positive_label)
        metrics.update({'fold': fold, 'trainRows': len(train), 'testRows': len(test)})
        fold_metrics.append(metrics)
        train_features = set(map(tuple, train[features].to_numpy()))
        duplicate_feature_rows += sum(tuple(row) in train_features for row in test[features].to_numpy())
        if group:
            group_overlap += len(set(train[group]) & set(test[group]))
        for offset, (actual, predicted) in enumerate(zip(test[target], prediction)):
            predictions.append({'rowIndex': int(test.index[offset]), 'fold': fold, 'yTrue': actual, 'yPred': predicted, 'score': score[offset] if score is not None else None})
    overall = score_predictions(
        np.array([row['yTrue'] for row in predictions]),
        np.array([row['yPred'] for row in predictions]),
        np.array([row['score'] for row in predictions]) if task == 'classification' and all(row['score'] is not None for row in predictions) else None,
        task, positive_label,
    )
    if task == 'classification':
        majority = data[target].mode().iloc[0]
        baseline_prediction = np.repeat(majority, len(data))
        baseline_score = np.repeat(float((data[target] == positive_label).mean()), len(data))
        baseline = score_predictions(data[target].to_numpy(), baseline_prediction, baseline_score, task, positive_label)
    else:
        baseline_prediction = np.repeat(data[target].mean(), len(data))
        baseline = score_predictions(data[target].to_numpy(), baseline_prediction, None, task, None)
    summary = {
        'rows': len(data), 'features': features, 'task': task, 'model': request.get('model', 'random_forest'),
        'foldCount': len(fold_metrics), 'grouped': bool(group), 'groupColumn': group,
        'metrics': overall, 'baseline': baseline,
        'leakageChecks': {'duplicateFeatureRowsAcrossFolds': duplicate_feature_rows, 'groupOverlapCount': group_overlap, 'targetExcludedFromFeatures': True},
    }
    batch_id = f"{datetime.now(timezone.utc).strftime('%Y%m%dT%H%M%SZ')}-{uuid.uuid4().hex[:8]}"
    output_root = state_root(request) / batch_id
    output_root.mkdir(parents=True, exist_ok=False)
    predictions_path = output_root / 'predictions.csv'
    summary_path = output_root / 'summary.json'
    manifest_path = output_root / 'manifest.json'
    pd.DataFrame(predictions).to_csv(predictions_path, index=False, encoding='utf-8-sig')
    summary_path.write_text(json.dumps(json_safe({'summary': summary, 'folds': fold_metrics}), ensure_ascii=False, indent=2), encoding='utf-8')
    manifest = {'batchId': batch_id, 'createdUtc': datetime.now(timezone.utc).isoformat().replace('+00:00', 'Z'), 'table': str(path), 'parameters': {'model': request.get('model', 'random_forest'), 'cvFolds': folds, 'task': task, 'features': features, 'groupColumn': group}, 'summary': summary, 'outputs': {'predictions': hash_file(predictions_path, request['maxHashBytes']), 'summary': hash_file(summary_path, request['maxHashBytes'])}}
    manifest_path.write_text(json.dumps(json_safe(manifest), ensure_ascii=False, indent=2), encoding='utf-8')
    return {'ok': True, 'action': 'evaluate', 'batchId': batch_id, 'summary': summary, 'folds': fold_metrics, 'predictions': predictions[:100], 'paths': {'predictions': str(predictions_path), 'summary': str(summary_path), 'manifest': str(manifest_path)}, 'statePaths': {'predictions': f'state://{batch_id}/predictions.csv', 'summary': f'state://{batch_id}/summary.json', 'manifest': f'state://{batch_id}/manifest.json'}}


def importance(request: dict[str, Any]) -> dict[str, Any]:
    path, data, features, target, task, group = prepare_data(request)
    positive_label = request.get('positiveLabel')
    if task == 'classification' and not isinstance(positive_label, str):
        raise ModelError('positiveLabel is required for classification')
    splitter = list(split_indices(data, features, target, task, group, int(number(request, 'cvFolds', 5, 2, 20))))
    train_index, test_index = splitter[-1]
    train = data.iloc[train_index]
    test = data.iloc[test_index]
    model = make_model(request, task)
    model.fit(train[features], train[target])
    result = permutation_importance(model, test[features], test[target], n_repeats=int(number(request, 'nRepeats', 5, 1, 100)), random_state=int(number(request, 'randomState', 42, 0, 100000000)), scoring='accuracy' if task == 'classification' else 'r2')
    rows = sorted([{'feature': feature, 'importanceMean': float(result.importances_mean[index]), 'importanceStd': float(result.importances_std[index])} for index, feature in enumerate(features)], key=lambda row: row['importanceMean'], reverse=True)
    batch_id = f"{datetime.now(timezone.utc).strftime('%Y%m%dT%H%M%SZ')}-{uuid.uuid4().hex[:8]}"
    output_root = state_root(request) / batch_id
    output_root.mkdir(parents=True, exist_ok=False)
    table_path = output_root / 'importance.csv'
    manifest_path = output_root / 'manifest.json'
    pd.DataFrame(rows).to_csv(table_path, index=False, encoding='utf-8-sig')
    manifest = {'batchId': batch_id, 'createdUtc': datetime.now(timezone.utc).isoformat().replace('+00:00', 'Z'), 'method': 'permutation_importance', 'notShap': True, 'table': str(path), 'features': features, 'rows': len(data), 'outputs': {'importance': hash_file(table_path, request['maxHashBytes'])}}
    manifest_path.write_text(json.dumps(json_safe(manifest), ensure_ascii=False, indent=2), encoding='utf-8')
    return {'ok': True, 'action': 'importance', 'batchId': batch_id, 'summary': {'method': 'permutation_importance', 'notShap': True, 'features': rows}, 'paths': {'importance': str(table_path), 'manifest': str(manifest_path)}, 'statePaths': {'importance': f'state://{batch_id}/importance.csv', 'manifest': f'state://{batch_id}/manifest.json'}}


def backtest(request: dict[str, Any]) -> dict[str, Any]:
    path, data, features, target, task, group = prepare_data(request)
    order_column = request.get('orderColumn')
    if not isinstance(order_column, str) or order_column not in data.columns:
        raise ModelError('orderColumn is required for backtest and must exist')
    data = data.sort_values(order_column)
    n = len(data)
    initial = max(2, int(n * number(request, 'initialTrainFraction', 0.6, 0.05, 0.95)))
    test_size = max(1, int(n * number(request, 'testFraction', 0.2, 0.01, 0.5)))
    step = max(1, int(n * number(request, 'stepFraction', 0.2, 0.01, 1.0)))
    folds = []
    predictions = []
    train_end = initial
    fold = 0
    while train_end < n:
        test_end = min(train_end + test_size, n)
        if test_end <= train_end:
            break
        fold += 1
        train = data.iloc[:train_end]
        test = data.iloc[train_end:test_end]
        model = make_model(request, task)
        model.fit(train[features], train[target])
        prediction, score = predict_model(model, test[features], task, request.get('positiveLabel'))
        metrics = score_predictions(test[target].to_numpy(), prediction, score, task, request.get('positiveLabel'))
        metrics.update({'fold': fold, 'trainRows': len(train), 'testRows': len(test)})
        folds.append(metrics)
        for offset, (actual, predicted) in enumerate(zip(test[target], prediction)):
            predictions.append({'rowIndex': int(test.index[offset]), 'fold': fold, 'yTrue': actual, 'yPred': predicted, 'score': score[offset] if score is not None else None})
        train_end += step
    if not folds:
        raise ModelError('No backtest folds were generated')
    overall = score_predictions(np.array([row['yTrue'] for row in predictions]), np.array([row['yPred'] for row in predictions]), np.array([row['score'] for row in predictions]) if task == 'classification' and all(row['score'] is not None for row in predictions) else None, task, request.get('positiveLabel'))
    batch_id = f"{datetime.now(timezone.utc).strftime('%Y%m%dT%H%M%SZ')}-{uuid.uuid4().hex[:8]}"
    output_root = state_root(request) / batch_id
    output_root.mkdir(parents=True, exist_ok=False)
    predictions_path = output_root / 'backtest_predictions.csv'
    manifest_path = output_root / 'manifest.json'
    pd.DataFrame(predictions).to_csv(predictions_path, index=False, encoding='utf-8-sig')
    summary = {'rows': n, 'foldCount': len(folds), 'metrics': overall, 'orderColumn': order_column, 'task': task}
    manifest = {'batchId': batch_id, 'createdUtc': datetime.now(timezone.utc).isoformat().replace('+00:00', 'Z'), 'summary': summary, 'folds': folds, 'outputs': {'predictions': hash_file(predictions_path, request['maxHashBytes'])}}
    manifest_path.write_text(json.dumps(json_safe(manifest), ensure_ascii=False, indent=2), encoding='utf-8')
    return {'ok': True, 'action': 'backtest', 'batchId': batch_id, 'summary': summary, 'folds': folds, 'predictions': predictions[:100], 'paths': {'predictions': str(predictions_path), 'manifest': str(manifest_path)}, 'statePaths': {'predictions': f'state://{batch_id}/backtest_predictions.csv', 'manifest': f'state://{batch_id}/manifest.json'}}


def self_test() -> dict[str, Any]:
    import tempfile
    checks = 0
    with tempfile.TemporaryDirectory(prefix='model-worker-') as temporary:
        root = Path(temporary)
        project = root / 'project'
        project.mkdir()
        state = root / 'state'
        rows = []
        for index in range(40):
            rows.append({'x': index / 10, 'group': index % 4, 'label': 'yes' if index % 10 >= 5 else 'no', 'order': index})
        table = project / 'data.csv'
        pd.DataFrame(rows).to_csv(table, index=False)
        base = {'table': 'data.csv', 'projectRoot': str(project), 'allowedRoots': [str(project)], 'stateRoot': str(state), 'featureColumns': ['x'], 'targetColumn': 'label', 'task': 'classification', 'positiveLabel': 'yes', 'model': 'logistic', 'maxHashBytes': 1000000}
        result = evaluate({**base, 'groupColumn': 'group', 'cvFolds': 2})
        assert result['summary']['foldCount'] == 2 and 'baseline' in result['summary']
        checks += 1
        importance_result = importance({**base, 'groupColumn': 'group', 'cvFolds': 2})
        assert importance_result['summary']['notShap'] is True
        checks += 1
        backtest_result = backtest({**base, 'orderColumn': 'order'})
        assert backtest_result['summary']['foldCount'] >= 1
        checks += 1
        try:
            evaluate({**base, 'table': '../escape.csv'})
        except ModelError:
            checks += 1
        else:
            raise AssertionError('Escaping table accepted')
    return {'ok': True, 'selfTest': 'passed', 'checks': checks}


def main() -> None:
    try:
        if '--self-test' in sys.argv:
            result = self_test()
        else:
            payload = sys.stdin.buffer.read(MAX_STDIN_BYTES + 1)
            if len(payload) > MAX_STDIN_BYTES:
                raise ModelError('stdin JSON exceeds the 4 MiB limit')
            request = json.loads(payload.decode('utf-8-sig'))
            if not isinstance(request, dict):
                raise ModelError('Worker request must be a JSON object')
            max_hash = request.get('maxHashBytes', 268435456)
            if not isinstance(max_hash, int) or isinstance(max_hash, bool) or not 1024 <= max_hash <= 2147483648:
                raise ModelError('maxHashBytes must be an integer from 1024 to 2147483648')
            request['maxHashBytes'] = max_hash
            action = request.get('action')
            if action == 'evaluate':
                result = evaluate(request)
            elif action == 'importance':
                result = importance(request)
            elif action == 'backtest':
                result = backtest(request)
            else:
                raise ModelError(f'Unsupported action: {action}')
    except ModelError as error:
        result = {'ok': False, 'error': {'code': 'MODEL_ERROR', 'message': str(error)}}
    except Exception as error:
        result = {'ok': False, 'error': {'code': 'MODEL_INTERNAL_ERROR', 'message': f'{type(error).__name__}: {str(error)[:4000]}'}}
    sys.stdout.buffer.write(json.dumps(json_safe(result), ensure_ascii=False, allow_nan=False).encode('utf-8'))


if __name__ == '__main__':
    main()
