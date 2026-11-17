// Teste de carga (k6) do requisito não funcional:
// "Em dias de pico, o consolidado recebe 50 req/s, com no máximo 5% de perda."
//
// Cenários:
//  1) pico_consolidado: 50 req/s constantes por 2 min  -> critério de aceite (thresholds)
//  2) estresse_2x:     100 req/s por 1 min             -> folga da arquitetura
//  3) lancamentos:     20 req/s de escrita em paralelo  -> leitura e escrita juntas
//
// Uso (ver README): k6 run -e TOKEN=<jwt> -e BASE_URL=http://localhost:8080 loadtest/consolidado-pico.js

import http from 'k6/http';
import { check } from 'k6';

const BASE = __ENV.BASE_URL || 'http://localhost:8080';
const TOKEN = __ENV.TOKEN;
const DATA = __ENV.DATA || new Date().toISOString().slice(0, 10);

if (!TOKEN) {
  throw new Error('defina TOKEN (gere com: make token)');
}

const auth = { Authorization: `Bearer ${TOKEN}` };

export const options = {
  scenarios: {
    pico_consolidado: {
      executor: 'constant-arrival-rate',
      rate: 50,
      timeUnit: '1s',
      duration: '2m',
      preAllocatedVUs: 20,
      maxVUs: 100,
      exec: 'consultar',
    },
    estresse_2x: {
      executor: 'constant-arrival-rate',
      rate: 100,
      timeUnit: '1s',
      duration: '1m',
      startTime: '2m10s',
      preAllocatedVUs: 40,
      maxVUs: 200,
      exec: 'consultar',
    },
    lancamentos: {
      executor: 'constant-arrival-rate',
      rate: 20,
      timeUnit: '1s',
      duration: '3m10s',
      preAllocatedVUs: 10,
      maxVUs: 50,
      exec: 'lancar',
    },
  },
  thresholds: {
    // Critério do desafio: perda <= 5%. Meta interna bem mais rígida (0,1%).
    'http_req_failed{scenario:pico_consolidado}': ['rate<0.05', 'rate<0.001'],
    'http_req_duration{scenario:pico_consolidado}': ['p(95)<200', 'p(99)<500'],
    // Requisições que o k6 nem conseguiu disparar também contam como perda.
    'dropped_iterations{scenario:pico_consolidado}': ['count<300'],
    'http_req_failed{scenario:lancamentos}': ['rate<0.001'],
  },
};

export function consultar() {
  const r = http.get(`${BASE}/v1/consolidado/diario/${DATA}`, { headers: auth, tags: { name: 'consolidado' } });
  check(r, { 'status 200': (res) => res.status === 200 });
}

export function lancar() {
  const corpo = JSON.stringify({
    tipo: Math.random() < 0.6 ? 'CREDITO' : 'DEBITO',
    valor: (Math.random() * 500 + 1).toFixed(2),
    descricao: 'carga k6',
  });
  const r = http.post(`${BASE}/v1/lancamentos`, corpo, {
    headers: {
      ...auth,
      'Content-Type': 'application/json',
      'Idempotency-Key': `k6-${__VU}-${__ITER}-${Date.now()}`,
    },
    tags: { name: 'lancamentos' },
  });
  check(r, { 'status 201': (res) => res.status === 201 });
}
