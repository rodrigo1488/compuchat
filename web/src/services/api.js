import axios from "axios";
import { getBackendUrl } from "../config/backendUrl";

const api = axios.create({
	baseURL: getBackendUrl(),
	withCredentials: true,
});

let onAuthLost = () => {};

export const setOnAuthLost = (handler) => {
	onAuthLost = typeof handler === "function" ? handler : () => {};
};

const readToken = () => {
	const stored = localStorage.getItem("token");
	if (!stored) return null;
	try {
		return JSON.parse(stored);
	} catch (err) {
		return stored;
	}
};

const clearSession = () => {
	localStorage.removeItem("token");
	localStorage.removeItem("companyId");
	localStorage.removeItem("ai_chat_messages");
	api.defaults.headers.Authorization = undefined;
	onAuthLost();
};

let isRefreshing = false;
let failedRequestsQueue = [];

// Cardápio e rotas públicas: bust de cache via query (evita preflight CORS por headers custom em GET).
// Headers Cache-Control/Pragma no *pedido* tornam o GET "não-simples" e quebram em aba anônima se o CORS não listar esses headers.
api.interceptors.request.use((config) => {
	const u = config.url || "";
	if (u.includes("/public/")) {
		const method = (config.method || "get").toLowerCase();
		if (method === "get") {
			config.params = { ...(config.params || {}), _: Date.now() };
		}
	}
	const token = readToken();
	if (token) {
		config.headers["Authorization"] = `Bearer ${token}`;
	}
	// Recalcula baseURL a cada request (hostname pode mudar em dev na rede)
	config.baseURL = getBackendUrl();
	return config;
});

api.interceptors.response.use(
	(response) => response,
	async (error) => {
		const originalRequest = error.config;
		if (!originalRequest) {
			return Promise.reject(error);
		}

		const requestUrl = originalRequest.url || "";
		if (requestUrl.includes("/auth/refresh_token") || requestUrl.includes("/auth/login")) {
			if (
				requestUrl.includes("/auth/refresh_token") &&
				(error?.response?.status === 401 || error?.response?.status === 403)
			) {
				clearSession();
			}
			return Promise.reject(error);
		}

		if (error?.response?.status === 403 && !originalRequest._retry) {
			if (isRefreshing) {
				return new Promise((resolve, reject) => {
					failedRequestsQueue.push({ resolve, reject });
				})
					.then((token) => {
						originalRequest.headers.Authorization = `Bearer ${token}`;
						return api(originalRequest);
					})
					.catch((err) => Promise.reject(err));
			}

			originalRequest._retry = true;
			isRefreshing = true;

			try {
				const { data } = await api.post("/auth/refresh_token");
				if (data) {
					localStorage.setItem("token", JSON.stringify(data.token));
					api.defaults.headers.Authorization = `Bearer ${data.token}`;
					failedRequestsQueue.forEach((request) => {
						request.resolve(data.token);
					});
					failedRequestsQueue = [];
				}
				return api(originalRequest);
			} catch (refreshError) {
				failedRequestsQueue.forEach((request) => {
					request.reject(refreshError);
				});
				failedRequestsQueue = [];
				clearSession();
				return Promise.reject(refreshError);
			} finally {
				isRefreshing = false;
			}
		}

		if (
			error?.response?.status === 401 ||
			(error?.response?.status === 403 && originalRequest._retry)
		) {
			clearSession();
		}

		return Promise.reject(error);
	}
);

export const openApi = axios.create({
	baseURL: getBackendUrl()
});

openApi.interceptors.request.use((config) => {
	config.baseURL = getBackendUrl();
	return config;
});

export default api;
