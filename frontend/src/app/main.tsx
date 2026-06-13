import { StrictMode } from 'react';
import { createRoot } from 'react-dom/client';
import { RouterProvider } from 'react-router';

import { AppProviders } from './providers';
import { createAppRouter } from './router';
import './theme.css';

const container = document.getElementById('root');
if (!container) {
  throw new Error('DataForge boot failure: the #root element is missing from index.html.');
}

createRoot(container).render(
  <StrictMode>
    <AppProviders>
      <RouterProvider router={createAppRouter()} />
    </AppProviders>
  </StrictMode>,
);

// CI pipeline verification touch (phase-01 exit criterion 3); harmless no-op.
