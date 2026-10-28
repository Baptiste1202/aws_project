"""Identifiants uniques des données de test (produits, codes promo).

Chaque lancement des scénarios crée ses propres produits et codes promo, avec un
suffixe aléatoire de 9 chiffres (un milliard de valeurs) : aucun test ne retombe sur
les données d'un lancement précédent ni sur celles d'une autre solution (le bucket
d'audit est commun aux trois). Une collision reste détectée à la création (409) et
un autre identifiant est alors tiré.
"""

import secrets


def unique_digits():
    return f"{secrets.randbelow(10 ** 9):09d}"


def product_id(prefix):
    """Identifiant produit au format du catalogue (shop/domain.py) : AA-nnnnnnnnn."""
    return f"{prefix}-{unique_digits()}"
