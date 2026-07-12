import { api } from "./client";
import { ENDPOINTS } from "./config";
import type {
  ModelCatalogResponse,
  ProviderModelCatalogResponse,
  UpdateProviderModelsRequest,
} from "./types";

/** Return the grouped model catalog used by the dashboard setup wizard. */
export function getModelCatalog(refresh = false): Promise<ModelCatalogResponse> {
  return api.get<ModelCatalogResponse>(
    ENDPOINTS.models.catalog,
    refresh ? { refresh: true } : undefined,
  );
}

export function updateProviderModels(
  provider: string,
  models: ReadonlyArray<string>,
): Promise<ProviderModelCatalogResponse> {
  const payload: UpdateProviderModelsRequest = { models };
  return api.put<ProviderModelCatalogResponse>(ENDPOINTS.models.provider(provider), payload);
}

export function resetProviderModels(provider: string): Promise<ProviderModelCatalogResponse> {
  return api.delete<ProviderModelCatalogResponse>(ENDPOINTS.models.provider(provider));
}
