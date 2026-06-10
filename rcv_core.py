"""
rcv_core.py
-----------
Instant-Runoff Voting (IRV / Maine RCV) tabulation engine for the
Maine 2026 Governor primary analyzer.

We only have *first-choice* town-level counts, so true ranked ballots do
not exist. Instead, when a candidate is eliminated we redistribute their
ballots using one of several transparent, configurable *transfer models*:

  * "town_proxy"   - In each town, an eliminated candidate's ballots flow
                     to that town's strongest *remaining* candidate (by
                     original first-choice strength). This is the
                     "voters rank their local top-3" proxy assumption.
  * "proportional" - Eliminated ballots split among remaining candidates
                     in proportion to their current round totals.
  * "matrix"       - A user-supplied pairwise transfer matrix
                     (from-candidate -> {to-candidate: share}).

Every model supports a per-candidate *exhaustion rate*: the share of an
eliminated candidate's ballots that rank no remaining candidate and drop
out of the count ("exhausted ballots").

Maine rule implemented: eliminate the lowest candidate each round; a
candidate wins once they hold a majority (>50%) of *continuing*
(non-exhausted) ballots.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Dict, List, Optional

import numpy as np
import pandas as pd


# --------------------------------------------------------------------------
# Result containers
# --------------------------------------------------------------------------
@dataclass
class RoundResult:
    """Snapshot of a single tabulation round."""

    round_number: int
    totals: Dict[str, float]               # active candidate -> votes (this round)
    eliminated: Optional[str] = None        # candidate eliminated at end of round
    transfers: Dict[str, float] = field(default_factory=dict)  # to-candidate -> votes received
    exhausted_this_round: float = 0.0       # ballots exhausted during this elimination
    exhausted_cumulative: float = 0.0       # running exhausted total
    winner: Optional[str] = None            # set on the final round if majority reached
    continuing_ballots: float = 0.0         # non-exhausted ballots this round


@dataclass
class RCVResult:
    """Full outcome of an IRV run."""

    candidates: List[str]
    rounds: List[RoundResult]
    winner: Optional[str]
    transfer_model: str
    total_first_round: float
    majority_threshold_final: float

    # ---- convenience views -------------------------------------------------
    def round_table(self) -> pd.DataFrame:
        """Round-by-round totals as a tidy DataFrame (one column per round)."""
        data: Dict[str, List[Optional[float]]] = {c: [] for c in self.candidates}
        data["Exhausted"] = []
        cols = []
        for r in self.rounds:
            cols.append(f"Round {r.round_number}")
            for c in self.candidates:
                data[c].append(r.totals.get(c, None))
            data["Exhausted"].append(r.exhausted_cumulative)
        df = pd.DataFrame(data, index=cols).T
        return df

    def summary_line(self) -> str:
        if self.winner is None:
            return "No majority winner could be determined."
        final = self.rounds[-1]
        share = (
            100.0 * final.totals.get(self.winner, 0) / final.continuing_ballots
            if final.continuing_ballots
            else 0.0
        )
        return (
            f"Projected winner: {self.winner} "
            f"({final.totals.get(self.winner, 0):,.0f} votes, "
            f"{share:.1f}% of continuing ballots in the final round)."
        )


# --------------------------------------------------------------------------
# Core tabulator
# --------------------------------------------------------------------------
def run_irv(
    town_matrix: pd.DataFrame,
    candidates: List[str],
    transfer_model: str = "town_proxy",
    exhaustion: Optional[Dict[str, float]] = None,
    default_exhaustion: float = 0.10,
    transfer_matrix: Optional[pd.DataFrame] = None,
) -> RCVResult:
    """Run an instant-runoff tabulation.

    Parameters
    ----------
    town_matrix : DataFrame
        One row per town, one column per candidate, holding first-choice
        vote counts. Index/other columns are ignored.
    candidates : list[str]
        Candidate columns to include in the contest (order preserved).
    transfer_model : {"town_proxy", "proportional", "matrix"}
        How eliminated ballots are redistributed.
    exhaustion : dict, optional
        Per-candidate exhaustion rate (0-1). Missing candidates use
        ``default_exhaustion``.
    default_exhaustion : float
        Fallback exhaustion rate.
    transfer_matrix : DataFrame, optional
        Required for the "matrix" model. Rows = from-candidate,
        columns = to-candidate, values = share (each row should sum to
        <= 1; the remainder is treated as exhausted).
    """
    exhaustion = exhaustion or {}
    cand = [c for c in candidates if c in town_matrix.columns]

    # Town-level working matrix of *active* ballots (float for transfers).
    M = town_matrix[cand].fillna(0).astype(float).copy()
    # Original first-choice strength, used by the town_proxy ranking.
    original = M.copy()

    total_first_round = float(M.values.sum())
    active = list(cand)
    rounds: List[RoundResult] = []
    exhausted_cumulative = 0.0
    winner: Optional[str] = None
    round_no = 0

    # Guard against pathological inputs.
    if total_first_round <= 0 or not active:
        return RCVResult(cand, [], None, transfer_model, 0.0, 0.0)

    while True:
        round_no += 1
        totals = {c: float(M[c].sum()) for c in active}
        continuing = sum(totals.values())
        threshold = continuing / 2.0

        rr = RoundResult(
            round_number=round_no,
            totals=dict(totals),
            exhausted_cumulative=exhausted_cumulative,
            continuing_ballots=continuing,
        )

        leader = max(totals, key=totals.get) if totals else None

        # Win condition: strict majority of continuing ballots, OR only one
        # candidate left standing.
        if leader is not None and (totals[leader] > threshold or len(active) == 1):
            rr.winner = leader
            winner = leader
            rounds.append(rr)
            break

        # Otherwise eliminate the weakest candidate (ties broken by smallest
        # original first-round count, then alphabetical for determinism).
        loser = min(active, key=lambda c: (totals[c], original[c].sum(), c))
        rr.eliminated = loser

        ex_rate = float(exhaustion.get(loser, default_exhaustion))
        ex_rate = min(max(ex_rate, 0.0), 1.0)

        remaining = [c for c in active if c != loser]

        transfers, exhausted_now = _redistribute(
            M=M,
            original=original,
            loser=loser,
            remaining=remaining,
            ex_rate=ex_rate,
            model=transfer_model,
            totals=totals,
            transfer_matrix=transfer_matrix,
        )

        exhausted_cumulative += exhausted_now
        rr.transfers = transfers
        rr.exhausted_this_round = exhausted_now
        rounds.append(rr)

        # Drop the eliminated candidate's column from the active set.
        M[loser] = 0.0
        active = remaining

        if len(active) == 1:
            # One left: record a final round showing the winner.
            round_no += 1
            c = active[0]
            final_total = float(M[c].sum())
            cont = final_total
            rounds.append(
                RoundResult(
                    round_number=round_no,
                    totals={c: final_total},
                    winner=c,
                    exhausted_cumulative=exhausted_cumulative,
                    continuing_ballots=cont,
                )
            )
            winner = c
            break

    final_threshold = rounds[-1].continuing_ballots / 2.0 if rounds else 0.0
    return RCVResult(
        candidates=cand,
        rounds=rounds,
        winner=winner,
        transfer_model=transfer_model,
        total_first_round=total_first_round,
        majority_threshold_final=final_threshold,
    )


def _redistribute(
    M: pd.DataFrame,
    original: pd.DataFrame,
    loser: str,
    remaining: List[str],
    ex_rate: float,
    model: str,
    totals: Dict[str, float],
    transfer_matrix: Optional[pd.DataFrame],
):
    """Move the loser's ballots onto remaining candidates (mutates M).

    Returns (transfers_dict, exhausted_votes).
    """
    loser_votes_total = float(M[loser].sum())
    if loser_votes_total <= 0 or not remaining:
        return {c: 0.0 for c in remaining}, loser_votes_total

    transfers = {c: 0.0 for c in remaining}

    if model == "town_proxy":
        # Per town, route the loser's ballots to the strongest remaining
        # candidate by original first-choice strength in that town.
        orig_rem = original[remaining]
        # Strongest remaining candidate per town (by original strength).
        best = orig_rem.idxmax(axis=1)
        # Towns where no remaining candidate had any original support -> the
        # ballots have nowhere to go and fully exhaust.
        has_target = orig_rem.sum(axis=1) > 0

        loser_col = M[loser]
        keep = 1.0 - ex_rate
        exhausted = 0.0
        for cand_to in remaining:
            mask = (best == cand_to) & has_target
            moved = float((loser_col[mask]).sum()) * keep
            M.loc[mask, cand_to] += loser_col[mask] * keep
            transfers[cand_to] += moved
        # Exhausted = ballots in no-target towns + the exhaustion slice.
        exhausted = (
            float(loser_col[~has_target].sum())
            + float(loser_col[has_target].sum()) * ex_rate
        )
        return transfers, exhausted

    if model == "matrix" and transfer_matrix is not None and loser in transfer_matrix.index:
        keep = 1.0 - ex_rate
        row = transfer_matrix.loc[loser]
        shares = {c: float(row.get(c, 0.0)) for c in remaining}
        s = sum(shares.values())
        if s <= 0:
            return transfers, loser_votes_total  # nothing specified -> exhaust
        moved_total = 0.0
        for c in remaining:
            frac = shares[c] / s * keep
            add = loser_votes_total * frac
            # Distribute proportionally across towns to keep town matrix sane.
            M[c] += M[loser] * frac
            transfers[c] += add
            moved_total += add
        return transfers, loser_votes_total - moved_total

    # Default / "proportional": split by current round strength of remaining.
    keep = 1.0 - ex_rate
    rem_strength = {c: totals.get(c, 0.0) for c in remaining}
    s = sum(rem_strength.values())
    if s <= 0:
        return transfers, loser_votes_total
    for c in remaining:
        frac = rem_strength[c] / s * keep
        M[c] += M[loser] * frac
        transfers[c] += loser_votes_total * frac
    return transfers, loser_votes_total * ex_rate


# --------------------------------------------------------------------------
# Town-level projected winner (runs a mini-IRV summary per town optional)
# --------------------------------------------------------------------------
def town_leaders(
    town_matrix: pd.DataFrame, candidates: List[str], town_col: str = "Town"
) -> pd.DataFrame:
    """Return first-choice leader and margin for each town."""
    cand = [c for c in candidates if c in town_matrix.columns]
    df = town_matrix.copy()
    sub = df[cand].fillna(0).astype(float)
    total = sub.sum(axis=1)
    leader = sub.idxmax(axis=1)
    top2 = sub.apply(lambda r: r.nlargest(2).values, axis=1)
    margin = top2.apply(lambda v: (v[0] - v[1]) if len(v) > 1 else v[0])// 1
    out = pd.DataFrame(
        {
            town_col: df[town_col] if town_col in df.columns else df.index,
            "Leader": leader.where(total > 0, "(no votes)"),
            "Total": total.astype(int),
            "Margin": margin.astype(int),
        }
    )
    out["Margin %"] = np.where(total > 0, (out["Margin"] / total * 100).round(1), 0.0)
    return out
