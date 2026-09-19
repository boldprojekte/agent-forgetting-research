// Copyright The OpenTelemetry Authors
// SPDX-License-Identifier: Apache-2.0

import type { NextApiRequest, NextApiResponse } from 'next';
import { context, Exception, Span, SpanStatusCode, trace } from '@opentelemetry/api';
import InstrumentationMiddleware from '../../utils/telemetry/InstrumentationMiddleware';
import CheckoutGateway from '../../gateways/rpc/Checkout.gateway';
import { Empty, PlaceOrderRequest } from '../../protos/demo';
import { IProductCheckoutItem, IProductCheckout } from '../../types/Cart';
import ProductCatalogService from '../../services/ProductCatalog.service';

type TResponse = IProductCheckout | Empty | { error: string; code?: string };

// The checkout service wraps every payment charge failure with this prefix
// (see src/checkout/main.go chargeCard). Anything else is an internal failure.
const PAYMENT_CHARGE_FAILURE_PREFIX = 'could not charge the card';

// Client-safe messages only: never echo upstream gRPC error details to the browser.
const PAYMENT_FAILED_ERROR = 'Payment failed: the payment method was declined.';
const INTERNAL_ERROR = 'Internal server error';

const isPaymentChargeFailure = (error: unknown) =>
  error instanceof Error && error.message.includes(PAYMENT_CHARGE_FAILURE_PREFIX);

const handler = async ({ method, body, query }: NextApiRequest, res: NextApiResponse<TResponse>) => {
  switch (method) {
    case 'POST': {
      const span = trace.getSpan(context.active()) as Span;

      try {
        const { currencyCode = '' } = query;
        const orderData = body as PlaceOrderRequest;
        const { order: { items = [], ...order } = {} } = await CheckoutGateway.placeOrder(orderData);

        const productList: IProductCheckoutItem[] = await Promise.all(
          items.map(async ({ item: { productId = '', quantity = 0 } = {}, cost }) => {
            const product = await ProductCatalogService.getProduct(productId, currencyCode as string);

            return {
              cost,
              item: {
                productId,
                quantity,
                product,
              },
            };
          })
        );

        return res.status(200).json({ ...order, items: productList });
      } catch (error) {
        // Keep the failure visible in telemetry even though we no longer rethrow.
        span.recordException(error as Exception);
        span.setStatus({ code: SpanStatusCode.ERROR });

        if (isPaymentChargeFailure(error)) {
          return res.status(422).json({ error: PAYMENT_FAILED_ERROR, code: 'PAYMENT_FAILED' });
        }

        return res.status(500).json({ error: INTERNAL_ERROR });
      }
    }

    default: {
      return res.status(405).send('');
    }
  }
};

export default InstrumentationMiddleware(handler);
