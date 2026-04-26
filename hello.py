from dataclasses import dataclass


@dataclass(frozen=True)
class ProjectSnapshot:
	name: str
	owner: str
	planned_progress: int
	actual_progress: int

	@property
	def variance(self) -> int:
		return self.actual_progress - self.planned_progress

	@property
	def health(self) -> str:
		if self.variance >= 5:
			return "Ahead"
		if self.variance >= -5:
			return "On Track"
		return "At Risk"


def format_portfolio_report(projects: list[ProjectSnapshot]) -> str:
	total_planned = sum(project.planned_progress for project in projects)
	total_actual = sum(project.actual_progress for project in projects)
	average_variance = round(
		sum(project.variance for project in projects) / len(projects),
		1,
	)

	lines = [
		"Portfolio Status Report",
		"=" * 24,
		f"Projects tracked: {len(projects)}",
		f"Total planned progress: {total_planned}%",
		f"Total actual progress: {total_actual}%",
		f"Average variance: {average_variance}%",
		"",
		"Project breakdown:",
	]

	for project in projects:
		sign = "+" if project.variance >= 0 else ""
		lines.append(
			f"- {project.name} ({project.owner}) -> {project.health} "
			f"[{project.actual_progress}% actual vs {project.planned_progress}% planned, {sign}{project.variance}%]"
		)

	return "\n".join(lines)


def main() -> None:
	sample_projects = [
		ProjectSnapshot("E-Commerce Platform", "Alice", 40, 35),
		ProjectSnapshot("Mobile App Redesign", "Carlos", 55, 48),
		ProjectSnapshot("Internal Tools", "Bob", 70, 72),
	]
	print(format_portfolio_report(sample_projects))


if __name__ == "__main__":
	main()
