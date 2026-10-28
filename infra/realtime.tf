# ---------------------------------------------------------------------------
# Suivi en temps réel (API Gateway WebSocket, facturée au message et à la minute de
# connexion, sans coût fixe ; commune aux trois solutions) :
#   navigateur --wss--> API WebSocket --> Lambda realtime --> table DynamoDB "realtime"
#                                          ($connect, subscribe, $disconnect)
#   SNS (ORDER_*, BACK_IN_STOCK) --> SQS realtime --> Lambda notifier --> @connections
# Le front garde le polling de l'API en secours (et seul en local, sans WebSocket).
# ---------------------------------------------------------------------------

resource "aws_dynamodb_table" "realtime" {
  name         = "${local.name}-realtime"
  billing_mode = "PAY_PER_REQUEST"
  hash_key     = "pk"
  range_key    = "sk"

  attribute {
    name = "pk"
    type = "S"
  }

  attribute {
    name = "sk"
    type = "S"
  }

  # Connexions et abonnements expirent seuls au bout de 2 h (attribut ttl).
  ttl {
    attribute_name = "ttl"
    enabled        = true
  }
}

resource "aws_apigatewayv2_api" "realtime" {
  name                       = "${local.name}-realtime"
  protocol_type              = "WEBSOCKET"
  route_selection_expression = "$request.body.action"
}

resource "aws_apigatewayv2_integration" "realtime" {
  api_id             = aws_apigatewayv2_api.realtime.id
  integration_type   = "AWS_PROXY"
  integration_uri    = aws_lambda_function.main["realtime"].invoke_arn
  integration_method = "POST"
}

resource "aws_apigatewayv2_route" "realtime" {
  for_each  = toset(["$connect", "$disconnect", "subscribe"])
  api_id    = aws_apigatewayv2_api.realtime.id
  route_key = each.value
  target    = "integrations/${aws_apigatewayv2_integration.realtime.id}"

  # subscribe renvoie un accusé de réception au navigateur (route response).
  route_response_selection_expression = each.value == "subscribe" ? "$default" : null
}

resource "aws_apigatewayv2_route_response" "subscribe" {
  api_id             = aws_apigatewayv2_api.realtime.id
  route_id           = aws_apigatewayv2_route.realtime["subscribe"].id
  route_response_key = "$default"
}

# Limites basses : chaque message appelle la Lambda (quota de 10 exécutions simultanées).
resource "aws_apigatewayv2_stage" "realtime" {
  api_id      = aws_apigatewayv2_api.realtime.id
  name        = "live"
  auto_deploy = true

  default_route_settings {
    throttling_burst_limit = 20
    throttling_rate_limit  = 10
  }
}

resource "aws_lambda_permission" "realtime" {
  statement_id  = "AllowApiGateway"
  action        = "lambda:InvokeFunction"
  function_name = aws_lambda_function.main["realtime"].function_name
  principal     = "apigateway.amazonaws.com"
  source_arn    = "${aws_apigatewayv2_api.realtime.execution_arn}/*"
}

locals {
  # Construites depuis l'API (pas depuis l'étape) : pas de cycle avec les Lambdas.
  realtime_url    = "wss://${aws_apigatewayv2_api.realtime.id}.execute-api.${var.region}.amazonaws.com/live"
  connections_url = "https://${aws_apigatewayv2_api.realtime.id}.execute-api.${var.region}.amazonaws.com/live"
}
