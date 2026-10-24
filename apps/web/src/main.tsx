import { StrictMode } from 'react';
import { createRoot } from 'react-dom/client';
import { App } from './App';
import { loadRuntimeConfig } from './config';
import './styles.css';

const root = createRoot(document.getElementById('root')!);

loadRuntimeConfig().then(
  (config) => root.render(<StrictMode><App config={config} /></StrictMode>),
  () => root.render(<p className="status">The application configuration could not be loaded.</p>),
);
