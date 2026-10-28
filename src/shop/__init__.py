"""Code commun aux trois solutions (Lambda et EC2).

  domain       : règles métier pures (validation, cycle de vie, événements)
  api          : routeur unique de l'API HTTP /api/*
  dynamo_store : accès aux données DynamoDB (serverless, lectures de l'hybride)
  sql_store    : accès aux données PostgreSQL (VM, écritures de l'hybride)
"""
