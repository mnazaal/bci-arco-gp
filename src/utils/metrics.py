from typing import Dict, Tuple

import numpy as np
import torch
from gadjid import ancestor_aid, parent_aid, oset_aid
from sklearn.metrics import precision_recall_curve, roc_curve, auc


def auc_scores(edge_probs: torch.Tensor, true_adj_mat: torch.Tensor, dag_to_cpdag: bool = False, score='roc'):
    assert edge_probs.squeeze().shape == true_adj_mat.squeeze().shape

    if dag_to_cpdag:
        # get undirected edges mask
        undir = true_adj_mat + true_adj_mat.T
        undir = torch.where(undir.int() == 2, 1, 0)
        # assemble edge probs
        undir_edge_probs = (edge_probs * undir) + (edge_probs * undir).T
        edge_probs = torch.where(undir.bool(), undir_edge_probs, edge_probs)
        # weight undirected edges only half since they occur twice & remove self loops
        weights = 1. - torch.eye(true_adj_mat.shape[-1]) - undir * 0.5
    else:
        edge_probs = edge_probs
        # do not consider self loops
        weights = (1. - torch.eye(true_adj_mat.shape[-1]))

    targets = true_adj_mat.int().view(-1).cpu().numpy()
    edge_probs = edge_probs.view(-1).cpu().numpy()
    weights = (weights + 1e-10).view(-1).cpu().numpy()  # + 1e-10 handles edge cases when no positives or negatives

    if score == 'roc':
        fpr, tpr, _ = roc_curve(targets, edge_probs, sample_weight=weights)
        return torch.tensor(auc(fpr, tpr)).float()
    elif score == 'prc':
        precision, recall, _ = precision_recall_curve(targets, edge_probs, sample_weight=weights)
        return torch.tensor(auc(recall, precision)).float()
    else:
        raise NotImplementedError


def auroc(edge_probs: torch.Tensor, target: torch.Tensor, cpdag_target: bool = False):
    return auc_scores(edge_probs, target, cpdag_target, score='roc')


def auprc(edge_probs: torch.Tensor, target: torch.Tensor, cpdag_target: bool = False):
    return auc_scores(edge_probs, target, cpdag_target, score='prc')


def edge_prediction_scores(target: torch.Tensor, prediction: torch.Tensor, dag_to_cpdag: bool = False):
    """Computes edge prediction scores for given edge probabilities and target graph.

    Parameters
    ----------
    target : torch.Tensor
        The adjacency matrix of the reference graph.
    prediction : torch.Tensor
        The adjacency matrix of the predicted graph or edge probabilities.
    dag_to_cpdag : bool
        If true, interpret the target as CPDAG and the prediction as DAG. In this case, count true undirected edges only
        as one positive that is correctly predicted if at least one direction is present.

    Returns
    ------
    Tuple[torch.Tensor]
        TPR, TNR, FNR, FPR, F1-Score, SHD
    """
    mask = (1. - torch.eye(target.shape[-1]))  # avoid counting self loops resulting in over-confident score

    if dag_to_cpdag:
        # count undirected edges only as one positive
        undir = target.triu() + target.tril().T
        undir = torch.where(undir.int() == 2, 1., 0.)
        undir = undir + undir.T
        p = target.sum() - undir.sum() * 0.5
    else:
        p = target.sum()

    p += 1e-10  # for edge cases where p = 0
    n = ((1. - target) * mask).sum() + 1e-10
    tp = (target * prediction).sum()
    fp = ((1. - target) * mask * prediction).sum()
    tn = n - fp
    fn = p - tp

    f1 = 2. * tp / (2. * tp + fp + fn)
    return tp / p, tn / n, fn / p, fp / n, f1, fn + fp


def compute_structure_metrics(target: torch.Tensor, prediction: torch.Tensor, dag_to_cpdag: bool = False) -> Dict[
    str, torch.Tensor]:
    """Computes edge prediction scores for given edge probabilities and target graph.

    Parameters
    ----------
    target : torch.Tensor
        The adjacency matrix of the reference graph.
    prediction : torch.Tensor
        The adjacency matrix of the predicted graph or edge probabilities.
    dag_to_cpdag : bool
        If true, interpret the target as CPDAG and the prediction as DAG. In this case, count true undirected edges only
        as one positive that is correctly predicted if at least one direction is present.

    Returns
    ------
    Dict[str, torch.Tensor]
        TPR, TNR, FNR, FPR, F1-Score, SHD
    """
    etpr, etnr, efnr, efpr, ef1, eshd = edge_prediction_scores(target, prediction, dag_to_cpdag)
    stats = {'auroc': auroc(prediction, target, dag_to_cpdag), 'auprc': auprc(prediction, target, dag_to_cpdag),
             'etpr': etpr, 'etnr': etnr, 'efnr': efnr, 'efpr': efpr, 'ef1': ef1, 'eshd': eshd}
    return stats


def shd(target: torch.Tensor, prediction: torch.Tensor, dag_to_cpdag: bool = False):
    """Computes the SHD between predicted and target graph (either DAGs or CPDAGs).

    Parameters
    ----------
    target : torch.Tensor
        The adjacency matrix of the reference graph.
    prediction : torch.Tensor
        The adjacency matrix of the predicted graph or edge probabilities.
    dag_to_cpdag : bool
        If true, interpret the target as CPDAG and the prediction as DAG. In this case, count true undirected edges only
        as one positive that is correctly predicted if at least one direction is present.

    Returns
    ------
    Dict[str, torch.Tensor]
        TPR, TNR, FNR, FPR, F1-Score, SHD
    """
    if dag_to_cpdag:
        # get undirected edges mask
        undir = target.triu() + target.tril().T
        undir = torch.where(undir.int() == 2, 1., 0.)
        undir = undir + undir.T
        # undirect prediction edges
        mixed_prediction = (prediction * undir) + (prediction * undir).T
        mixed_prediction = torch.where(undir.bool(), mixed_prediction, prediction)
        score = (target - mixed_prediction).abs()
        # avoid double-counting of undirected edges
        score = score - score * undir * 0.5
        score = score.sum()
    else:
        score = (target - prediction).abs().sum()

    return score


def aid(target: torch.Tensor, prediction: torch.Tensor, mode: str = 'ancestor'):
    """Computes an adjustment distance (Henkel 2024) between target and prediction.

    Parameters
    ----------
    target : torch.Tensor
        The adjacency matrix of the reference DAG or CPDAG.
    prediction : torch.Tensor
        The adjacency matrix of the predicted DAG or CPDAG.
    mode : str
        One of {ancestor, parent, oset} accoring to the adjustment distances proposed by (Henkel 2024).

    Returns
    ------
    torch.Tensor
        The normalised AID.
    """
    # gadjid requires undirected edges to be represented as 2s in the adjacency matrix
    undir_edges_mask = (target + target.T).int() == 2
    target = torch.where(undir_edges_mask, torch.tensor(2), target)
    target = target.numpy().astype(np.int8)

    # gadjid requires undirected edges to be represented as 2s in the adjacency matrix
    undir_edges_mask = (prediction + prediction.T).int() == 2
    prediction = torch.where(undir_edges_mask, torch.tensor(2), prediction)
    prediction = prediction.numpy().astype(np.int8)

    # compute the aid
    if mode == 'ancestor':
        distance, _ = ancestor_aid(target, prediction)
    elif mode == 'parent':
        distance, _ = parent_aid(target, prediction)
    elif mode == 'oset':
        distance, _ = oset_aid(target, prediction)
    else:
        print(f'Invalid AID mode {mode}. Using ancestor AID per default.')
        distance, _ = ancestor_aid(target, prediction)

    return torch.tensor(distance)