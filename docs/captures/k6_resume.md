Test de charge k6 (aws) : 8 utilisateurs virtuels, 60s à charge pleine (+ 10 s de montée), parcours de lecture.

| Solution | Requêtes | Req/s | Médiane | p95 | Max | Réponses attendues | 429 (quota Lambda) | 5xx |
|---|---|---|---|---|---|---|---|---|
| serverless | 1068 | 13.8 | 52 ms | 272 ms | 514 ms | 100.0 % | 0 | 0 |
| vm | 1110 | 14.5 | 56 ms | 241 ms | 390 ms | 100.0 % | 0 | 0 |
| hybrid | 1074 | 14.1 | 47 ms | 251 ms | 1110 ms | 100.0 % | 0 | 0 |

p95 par page (ms) :

| Solution | catalogue | recherche | fiche | avis | suivi | sante |
|---|---|---|---|---|---|---|
| serverless | 309 | 332 | 193 | 162 | 225 | 153 |
| vm | 266 | 251 | 261 | 199 | 257 | 101 |
| hybrid | 303 | 321 | 163 | 155 | 158 | 156 |
