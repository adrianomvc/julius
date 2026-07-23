"""Coletores boto3 ao vivo (MVP 2).

Populam os MESMOS dataclasses de `julius.inventory.model` que a ingestão de
dataset exportado usa — então detecção/scoring/relatório não mudam.

boto3 é dependência OPCIONAL (`pip install julius[aws]`). Os módulos daqui só
são importados quando a coleta ao vivo é solicitada; o caminho offline (dataset
exportado) não depende de boto3.
"""
