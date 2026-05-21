import axios from 'axios';

const browserProtocol = typeof window !== 'undefined' ? window.location.protocol : 'http:';
const browserHost = typeof window !== 'undefined' ? window.location.hostname : 'localhost';
const apiPort = import.meta.env.VITE_SDO_API_PORT || '9300';

const configuredApiBase = import.meta.env.VITE_SDO_API_BASE;
const configuredWsBase = import.meta.env.VITE_SDO_WS_BASE;

export const apiBase = (configuredApiBase || `${browserProtocol}//${browserHost}:${apiPort}`).replace(/\/$/, '');
export const wsBase = (
  configuredWsBase || `${browserProtocol === 'https:' ? 'wss:' : 'ws:'}//${browserHost}:${apiPort}`
).replace(/\/$/, '');

export const api = axios.create({
  baseURL: apiBase,
  timeout: 15000,
});

export function buildApiUrl(path) {
  return `${apiBase}${path.startsWith('/') ? path : `/${path}`}`;
}

export function buildWsUrl(path) {
  return `${wsBase}${path.startsWith('/') ? path : `/${path}`}`;
}
