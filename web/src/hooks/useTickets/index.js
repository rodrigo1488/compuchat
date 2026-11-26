import { useQuery } from "react-query";
import toastError from "../../errors/toastError";
import api from "../../services/api";

const EMPTY_TICKETS = [];

const useTickets = ({
  searchParam,
  tags,
  users,
  pageNumber,
  status,
  date,
  updatedAt,
  showAll,
  queueIds,
  withUnreadMessages,
}) => {
  const params = {
    searchParam,
    pageNumber,
    tags,
    users,
    status,
    date,
    updatedAt,
    showAll,
    queueIds,
    withUnreadMessages,
  };

  const { data, isLoading, isFetching } = useQuery(
    ["tickets", params],
    async () => {
      const { data: payload } = await api.get("/tickets", { params });
      return payload;
    },
    {
      staleTime: 15 * 1000,
      onError: (err) => toastError(err),
    }
  );

  return {
    tickets: data?.tickets || EMPTY_TICKETS,
    hasMore: Boolean(data?.hasMore),
    count: data?.count,
    loading: isLoading || (isFetching && !data),
  };
};

export default useTickets;
