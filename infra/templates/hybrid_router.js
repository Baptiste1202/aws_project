// CloudFront Function (runtime cloudfront-js-2.0) de la solution hybride, sur /api/*.
// Lectures (GET, HEAD, OPTIONS) : origine par défaut, la Lambda hybrid-read (DynamoDB).
// Écritures (POST, PUT, DELETE...) : origine "alb", l'EC2 + RDS (transactions SQL).
// L'origine "alb" garde ses en-têtes X-Origin-Verify et X-Shop, exigés par la règle ALB.
import cf from 'cloudfront';

const READS = ['GET', 'HEAD', 'OPTIONS'];

function handler(event) {
    if (READS.indexOf(event.request.method) === -1) {
        cf.selectRequestOriginById('alb');
    }
    return event.request;
}
