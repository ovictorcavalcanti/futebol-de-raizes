"""A classificação não tem página no Django Admin.

A tabela (`Standing`) é um cache que o sistema recalcula a partir das partidas, das
regras da fase e das punições/bonificações (standings/services.py): ninguém a edita à
mão. As regras e as punições (`PointAdjustment`) ficam na página da fase
(competitions/admin.py), onde também está a explicação de onde a tabela vem.
"""
