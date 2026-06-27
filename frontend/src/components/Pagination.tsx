interface PaginationProps {
  currentPage: number;
  totalPages: number;
  onPageChange: (page: number) => void;
}

const containerStyle: React.CSSProperties = {
  display: 'flex',
  alignItems: 'center',
  justifyContent: 'center',
  gap: '8px',
  padding: '16px 0',
};

const buttonStyle: React.CSSProperties = {
  padding: '6px 12px',
  border: '1px solid #d1d5db',
  borderRadius: '6px',
  background: '#ffffff',
  cursor: 'pointer',
  fontSize: '0.875rem',
  color: '#374151',
};

const activeButtonStyle: React.CSSProperties = {
  ...buttonStyle,
  background: '#3b82f6',
  color: '#ffffff',
  borderColor: '#3b82f6',
};

const disabledButtonStyle: React.CSSProperties = {
  ...buttonStyle,
  opacity: 0.5,
  cursor: 'not-allowed',
};

export function Pagination({ currentPage, totalPages, onPageChange }: PaginationProps) {
  if (totalPages <= 1) return null;

  const pages: number[] = [];
  const maxVisible = 5;
  let start = Math.max(1, currentPage - Math.floor(maxVisible / 2));
  const end = Math.min(totalPages, start + maxVisible - 1);
  start = Math.max(1, end - maxVisible + 1);

  for (let i = start; i <= end; i++) {
    pages.push(i);
  }

  return (
    <div style={containerStyle}>
      <button
        style={currentPage <= 1 ? disabledButtonStyle : buttonStyle}
        onClick={() => onPageChange(currentPage - 1)}
        disabled={currentPage <= 1}
      >
        Previous
      </button>
      {pages.map((page) => (
        <button
          key={page}
          style={page === currentPage ? activeButtonStyle : buttonStyle}
          onClick={() => onPageChange(page)}
        >
          {page}
        </button>
      ))}
      <button
        style={currentPage >= totalPages ? disabledButtonStyle : buttonStyle}
        onClick={() => onPageChange(currentPage + 1)}
        disabled={currentPage >= totalPages}
      >
        Next
      </button>
    </div>
  );
}
