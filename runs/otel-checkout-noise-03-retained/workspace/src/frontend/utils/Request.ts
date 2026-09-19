// Copyright The OpenTelemetry Authors
// SPDX-License-Identifier: Apache-2.0

interface IRequestParams {
  url: string;
  body?: object;
  method?: 'GET' | 'POST' | 'PUT' | 'DELETE';
  queryParams?: Record<string, any>;
  headers?: Record<string, string>;
}

const request = async <T>({
  url = '',
  method = 'GET',
  body,
  queryParams = {},
  headers = {
    'content-type': 'application/json',
  },
}: IRequestParams): Promise<T> => {
  const response = await fetch(`${url}?${new URLSearchParams(queryParams).toString()}`, {
    method,
    body: body ? JSON.stringify(body) : undefined,
    headers,
  });

  const responseText = await response.text();

  let parsedBody: unknown;
  if (responseText) {
    try {
      parsedBody = JSON.parse(responseText);
    } catch {
      // Non-JSON error bodies (e.g. plain-text proxy responses) are handled safely below.
      parsedBody = undefined;
    }
  }

  if (!response.ok) {
    const serverMessage =
      parsedBody && typeof parsedBody === 'object' && 'error' in parsedBody
        ? // eslint-disable-next-line @typescript-eslint/no-explicit-any
          (parsedBody as any).error
        : undefined;

    throw new Error(
      typeof serverMessage === 'string' && serverMessage.length > 0
        ? serverMessage
        : `Request failed with status ${response.status}`
    );
  }

  return (parsedBody ?? undefined) as T;
};

export default request;
