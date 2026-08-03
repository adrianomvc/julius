---
name: sagemaker
---

## sagemaker_app

- O trabalho executado justifica esse tipo de instância, ou é GPU parada?
- Isso exige Studio ativo, ou cabe num training/processing job sob demanda?

## sagemaker_training_job

- O script usa o acelerador que a instância cobra, ou só a biblioteca que o importa? Confirme contra o arquivo inteiro, não pelo import.
- O treino tolera interrupção com retomada por checkpoint? É essa resposta que libera ou barra o managed spot.
- As instâncias extras recebem trabalho, ou o script roda em uma só?
- O tempo entre o início cobrado e a primeira época é download de dado que FastFile ou Pipe evitariam?

## sagemaker_endpoint

- O consumo justifica inferência em tempo real, ou Serverless/Async atende?
- Quem chama este endpoint hoje, e esse consumidor ainda existe?
