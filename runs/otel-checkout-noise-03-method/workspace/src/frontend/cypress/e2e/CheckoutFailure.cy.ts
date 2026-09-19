// Copyright The OpenTelemetry Authors
// SPDX-License-Identifier: Apache-2.0

import { getElementByField } from '../../utils/Cypress';
import { CypressFields } from '../../utils/enums/CypressFields';

/**
 * Focused regression tests for the checkout failure contract under the
 * current controlled fault conditions:
 * - simulated payment declines return HTTP 422 with a client-safe,
 *   non-empty `error` and `code: "PAYMENT_FAILED"` (never a generic 500);
 * - unrelated internal failures remain generic HTTP 500 JSON responses with
 *   a non-empty `error`, without the PAYMENT_FAILED code or upstream details.
 */

const TEST_USER_ID = '00000000-0000-4000-8000-000000000001';

// The cart page does not render a user-facing error for a rejected placeOrder
// call, so the (expected) RequestError from the shared request helper surfaces
// as an unhandled promise rejection in the browser. The HTTP contract
// assertions below are what these tests verify.
Cypress.on('uncaught:exception', err => {
  if (err && err.name === 'RequestError') {
    return false;
  }

  return undefined;
});

const seedSession = () => {
  cy.visit('/');
  cy.window().then(window => {
    window.localStorage.setItem('session', JSON.stringify({ userId: TEST_USER_ID, currencyCode: 'USD' }));
  });
};

const addToCartViaApi = (productId: string, quantity = 1) => {
  cy.request({
    method: 'POST',
    url: '/api/cart?currencyCode=USD',
    body: { item: { productId, quantity }, userId: TEST_USER_ID },
  })
    .its('status')
    .should('equal', 200);
};

const emptyCartViaApi = () => {
  cy.request({
    method: 'DELETE',
    url: '/api/cart?currencyCode=USD',
    body: { userId: TEST_USER_ID },
    failOnStatusCode: false,
  });
};

const placeOrderAndIntercept = () => {
  cy.intercept('POST', '/api/checkout*').as('placeOrder');

  cy.visit('/cart');
  getElementByField(CypressFields.CheckoutPlaceOrder).click();

  return cy.wait('@placeOrder', { timeout: 20000 });
};

describe('Checkout failure contract', () => {
  it('returns 422 PAYMENT_FAILED for simulated payment declines', () => {
    const maxAttempts = 20;

    seedSession();
    emptyCartViaApi();
    addToCartViaApi('0PUK6V6EV0');

    const attempt = (n: number): void => {
      placeOrderAndIntercept().then(({ response }) => {
        const status = response?.statusCode ?? 0;

        expect(status, 'checkout must never surface a generic 500 for payment declines').to.not.equal(500);

        if (status === 422) {
          const body = response?.body as { error?: string; code?: string };

          expect(body?.code, 'payment decline must carry code PAYMENT_FAILED').to.equal('PAYMENT_FAILED');
          expect(body?.error, 'payment decline must carry a client-safe error message')
            .to.be.a('string')
            .and.not.empty;
          expect(body?.error, 'error must not leak upstream details').to.not.contain('grpc');
          expect(body?.error, 'error must not leak upstream details').to.not.contain('Invalid token');

          return;
        }

        // Payment declines are probabilistic under the active fault: a 200
        // success is still valid, keep trying up to the attempt limit.
        if (n >= maxAttempts) {
          throw new Error(`Expected a simulated payment decline within ${maxAttempts} attempts`);
        }

        attempt(n + 1);
      });
    };

    attempt(1);
  });

  it('keeps unrelated internal checkout failures as generic 500 JSON without PAYMENT_FAILED', () => {
    // The controlled product-catalog fault makes this product fail during
    // order preparation: an internal checkout failure unrelated to payment.
    // The failing product cannot be rendered in the cart UI, so the live
    // checkout API route is exercised directly with the same payload the
    // browser would send.
    seedSession();
    emptyCartViaApi();
    addToCartViaApi('OLJCESPC7Z');

    cy.request({
      method: 'POST',
      url: '/api/checkout?currencyCode=USD',
      failOnStatusCode: false,
      body: {
        userId: TEST_USER_ID,
        email: 'someone@example.com',
        address: {
          streetAddress: '1600 Amphitheatre Parkway',
          state: 'CA',
          country: 'United States',
          city: 'Mountain View',
          zipCode: '94043',
        },
        userCurrency: 'USD',
        creditCard: {
          creditCardCvv: 672,
          creditCardExpirationMonth: 1,
          creditCardExpirationYear: 2030,
          creditCardNumber: '4432-8015-6152-0454',
        },
      },
    }).then(response => {
      expect(response.status, 'non-payment internal failure stays a 500').to.equal(500);

      const body = response.body as { error?: string; code?: string };

      expect(body?.error, 'internal failure must carry a client-safe error').to.be.a('string').and.not.empty;
      expect(body?.code, 'internal failure must not be classified as a payment failure').to.not.equal(
        'PAYMENT_FAILED'
      );
      expect(JSON.stringify(body), 'internal failure must not leak upstream details').to.not.contain('grpc');
      expect(JSON.stringify(body), 'internal failure must not leak upstream details').to.not.contain('Invalid token');
    });
  });

  it('handles non-JSON error bodies safely in the shared request helper', () => {
    seedSession();

    cy.intercept('GET', '/api/cart*', {
      statusCode: 502,
      headers: { 'content-type': 'text/html' },
      body: '<html><body>Bad Gateway</body></html>',
    }).as('getCart');

    cy.visit('/cart');

    // The helper must reject the non-success response without throwing a
    // JSON.parse error; the cart page still renders.
    cy.wait('@getCart', { timeout: 20000 });
    cy.contains(/cart/i).should('exist');
  });
});

