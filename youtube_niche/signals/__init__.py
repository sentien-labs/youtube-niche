"""Signal modules. Each returns normalized [0,1] sub-scores plus an explainable detail dict.

A  outlier      — views vs subscribers (beatability / topic-carried hits)
B  supply_age   — how stale the top results are
C  competition  — how few credible results exist
D  small_channel— how many small channels rank
E  comments     — unmet viewer demand in comments
F  trends       — rising interest (Google Trends, YouTube source)
G  quality      — how thin the top-ranking content is
"""
