// QuickChips：推荐问法来自 /api/meta（golden 题集现读），点一下只填输入框、不直接提交
export function QuickChips({ questions, onPick }: { questions: string[]; onPick: (q: string) => void }) {
  if (questions.length === 0) return null
  return (
    <div className="chips quick">
      {questions.map((q, i) => (
        <button type="button" key={i} className="chip" onClick={() => onPick(q)}>
          {q}
        </button>
      ))}
    </div>
  )
}
