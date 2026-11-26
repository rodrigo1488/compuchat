import { useQuery } from "react-query";
import { useDebounce } from "use-debounce";
import toastError from "../../errors/toastError";
import api from "../../services/api";

const EMPTY_CONTACTS = [];

const useContacts = ({ searchParam, pageNumber, date, dateStart, dateEnd }) => {
  const [debouncedSearch] = useDebounce(searchParam, searchParam ? 400 : 0);
  const params = {
    searchParam: debouncedSearch,
    pageNumber,
    date,
    dateStart,
    dateEnd,
  };

  const { data, isLoading, isFetching } = useQuery(
    ["contacts", params],
    async () => {
      const { data: payload } = await api.get("/contacts", { params });
      return payload;
    },
    {
      staleTime: 30 * 1000,
      keepPreviousData: true,
      onError: (err) => toastError(err),
    }
  );

  return {
    contacts: data?.contacts || EMPTY_CONTACTS,
    hasMore: Boolean(data?.hasMore),
    count: data?.count || 0,
    loading: isLoading || (isFetching && !data),
  };
};

export default useContacts;
